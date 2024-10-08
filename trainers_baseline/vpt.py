import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.cuda.amp import GradScaler, autocast

from dassl.engine import TRAINER_REGISTRY
from dassl.metrics import compute_accuracy
from dassl.utils import load_pretrained_weights, count_num_param
from dassl.optim import build_optimizer, build_lr_scheduler

from clip import clip

from trainers_baseline.basedg import *
from utils.clip_part import *
from utils.templates import CUSTOM_TEMPLATES
from utils.visual_prompt import *


class PromptLearner(Base_PromptLearner):
    def __init__(self, cfg, classnames, clip_model):
        super().__init__(cfg, classnames, clip_model)
        dtype = clip_model.dtype
        self.num_tokens = cfg.TRAINER.VPT.NUM_TOKENS    # number of prompted tokens
        prompt_dim = cfg.MODEL.HIDDEN_SIZE
        self.prompt_dropout = nn.Dropout(cfg.TRAINER.VPT.DROPOUT)
        self.location = cfg.TRAINER.VPT.LOCATION
        self.deep_layer = cfg.TRAINER.VPT.DEEP_LAYERS
        
        self.vctx = None
        self.deep_vctx = None
        if not cfg.TRAINER.VPT.ENABLE_CONV:
            if cfg.TRAINER.VPT.VP:
                vctx_vectors = torch.empty(self.num_tokens, prompt_dim)
                nn.init.normal_(vctx_vectors, std=0.02)
                self.vctx = nn.Parameter(vctx_vectors)
            
            if cfg.TRAINER.VPT.V_DEEP:  
                if self.deep_layer == None:
                    deep_vctx_vectors = torch.empty(cfg.MODEL.NUM_LAYER - 1, self.num_tokens, prompt_dim)
                    nn.init.normal_(deep_vctx_vectors, std=0.02)
                else:
                    deep_ctx_vectors = torch.empty(self.deep_layer[1] - self.deep_layer[0] + 1, self.num_tokens, prompt_dim)
                    nn.init.normal_(deep_ctx_vectors, std=0.02)
                self.deep_vctx = nn.Parameter(deep_vctx_vectors)
        
        else:
            if cfg.TRAINER.VPT.TYPE == "random":
                random_prompter = RandomPatchPrompter(cfg)
                self.prompter = random_prompter
            elif cfg.TRAINER.VPT.TYPE == "fix":
                fix_prompter = FixedPatchPrompter(cfg)
                self.prompter = fix_prompter
            elif cfg.TRAINER.VPT.TYPE == "pad":
                pad_prompter = PadPrompter(cfg)
                self.prompter = pad_prompter
            else:
                raise ValueError('Conv VPT type is wrong!')
        
        prompt_prefix = CUSTOM_TEMPLATES[cfg.DATASET.NAME]
        prompts = [prompt_prefix.format(c.replace("_", " ")) for c in classnames]
        tokenized_prompts = clip.tokenize(prompts)
        with torch.no_grad():
            embedding = clip_model.token_embedding(tokenized_prompts).type(dtype)
        self.ctx = embedding.to(torch.device("cuda:{}".format(cfg.GPU))) 
        self.tokenized_prompts = tokenized_prompts
                
    def forward(self, image=None):
        if image == None:
            return self.ctx, None, self.vctx, self.deep_vctx
        else:
            return self.ctx, None, self.vctx, self.deep_vctx, self.prompter(image)


class CustomCLIP(Base_CustomCLIP):
    def __init__(self, cfg, classnames, clip_model):
        super().__init__(cfg, classnames, clip_model)
        self.prompt_learner = PromptLearner(cfg, classnames, clip_model)
        self.tokenized_prompts = self.prompt_learner.tokenized_prompts
        
        self.text_encoder = TextEncoder(cfg, clip_model, self.prompt_learner)

        if cfg.MODEL.BACKBONE.NAME.split('-')[0] == 'ViT':
            self.image_encoder = ImageEncoder_Trans(cfg, clip_model, self.prompt_learner)
        else:  # RN50, RN101
            self.image_encoder = ImageEncoder_Conv(cfg, clip_model, self.prompt_learner)
            
        self.logit_scale = clip_model.logit_scale
        self.dtype = clip_model.dtype
        self.conv = cfg.TRAINER.VPT.ENABLE_CONV

    def forward(self, image, prompt=False):
        if self.conv:
            prompts, deep_ctx, vctx, deep_vctx, image = self.prompt_learner(image)
        else:
            prompts, deep_ctx, vctx, deep_vctx = self.prompt_learner()
        
        text_features = self.text_encoder(prompts, self.tokenized_prompts, deep_ctx) 
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)

        if prompt:
            image_features = self.image_encoder(image.type(self.dtype), None, None) 
        else:   
            image_features = self.image_encoder(image.type(self.dtype), vctx, deep_vctx)    
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)

        logit_scale = self.logit_scale.exp()
        logits = logit_scale * image_features @ text_features.t()

        return logits
    

@TRAINER_REGISTRY.register()
class VPT(BaseDG):
    '''Visual Prompt Tuning (VPT)
    
    Adapt from Visual Prompt Tuning
    https://arxiv.org/pdf/2203.12119.pdf
    '''
    def build_model(self):
        cfg = self.cfg
        classnames = self.dm.dataset.classnames

        if torch.cuda.is_available() and cfg.USE_CUDA:
            if len(cfg.GPU) == 1:
                self.device = torch.device("cuda:{}".format(cfg.GPU))
            else:
                self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")
            
        self.best_test_result = -np.inf
        self.best_val_test_result = -np.inf

        print(f"Loading CLIP (backbone: {cfg.MODEL.BACKBONE.NAME})")
        clip_model = load_clip_to_cpu(cfg)

        if cfg.TRAINER.VPT.PREC == "fp32" or cfg.TRAINER.VPT.PREC == "amp":
            clip_model.float()  # CLIP's default precision is fp16

        print("Building custom CLIP...")
        self.model = CustomCLIP(cfg, classnames, clip_model)

        print("Turning off gradients in both the image and the text encoder...")
        for name, param in self.model.named_parameters():
            if "prompt_learner" not in name:
                param.requires_grad_(False)
                
        # Double check
        enabled = set()
        for name, param in self.model.named_parameters():
            if param.requires_grad:
                enabled.add(name)
        print(f"Parameters to be updated: {sorted(enabled)}")
        print("# params: {:,}".format(count_num_param(self.model.prompt_learner)))

        # if cfg.MODEL.INIT_WEIGHTS:
        #     load_pretrained_weights(self.model.prompt_learner, cfg.MODEL.INIT_WEIGHTS)
 
        self.model.to(self.device)
        
        # NOTE: only give prompt_learner to the optimizer
        self.optim = build_optimizer(self.model.prompt_learner, cfg.OPTIM)
        self.sched = build_lr_scheduler(self.optim, cfg.OPTIM)
        self.register_model("prompt_learner", self.model.prompt_learner, self.optim, self.sched)
        self.scaler = GradScaler() if cfg.TRAINER.VPT.PREC == "amp" else None

    def forward_backward(self, batch):
        images, labels = self.parse_batch_train(batch)
        prec = self.cfg.TRAINER.VPT.PREC
        
        if prec == "amp":
            with autocast():
                output = self.model(images)
                loss = F.cross_entropy(output, labels)
            self.optim.zero_grad()
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optim)
            self.scaler.update()
        else:
            output = self.model(images)
            loss = F.cross_entropy(output, labels)
            self.model_backward_and_update(loss)
            
        loss_summary = {
            "loss": loss.item(),
            "acc": compute_accuracy(output, labels)[0].item(),
        }
        
        if (self.batch_idx + 1) == self.num_batches:
            self.update_lr()

        return loss_summary
