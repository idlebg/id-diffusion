import itertools
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint
import diffusers
import lightning.pytorch as pl
from lib.model import StableDiffusionModel, get_class, min_snr_weighted_loss

class LoConBaseModel(torch.nn.Module):
    
    def __init__(self, unet, text_encoder, config):

        super().__init__()
        names = []
        self.multiplier, self.r, alpha, dropout = config.multipier, config.rank, config.lora_alpha, config.get("dropout", 0.0)
        self.conv_r, self.conv_alpha = getattr(config, "conv_rank", self.r), getattr(config, "conv_alpha", alpha)
        
        self.text_encoder_loras = self.create_modules('lora_te', text_encoder, ["CLIPAttention", "CLIPMLP"], alpha, dropout)
        
        unet_modules = ["Transformer2DModel", "Attention", "ResnetBlock2D", "Downsample2D", "Upsample2D"]
        if diffusers.__version__ >= "0.15.0":
            unet_modules = ["Transformer2DModel", "ResnetBlock2D", "Downsample2D", "Upsample2D"]
        self.unet_loras = self.create_modules('lora_unet', unet, unet_modules, alpha, dropout)
            
        print(f"create LoCon for Text Encoder: {len(self.text_encoder_loras)} modules.")
        print(f"create LoCon for U-Net: {len(self.unet_loras)} modules.")
        
        names = set()
        for lora in self.text_encoder_loras + self.unet_loras:
            assert lora.lora_name not in names, f"duplicated lora name: {lora.lora_name}"
            names.add(lora.lora_name)
            
    def create_modules(self, prefix, model, target_replace_module, alpha, dropout):
        if model is None:
            return []
        
        blocks = []
        for name, module in model.named_modules():
            if module.__class__.__name__ in target_replace_module:
                for child_name, child_module in module.named_modules():
                    lora_name = prefix + '.' + name + '.' + child_name
                    lora_name = lora_name.replace('.', '_')
                    if child_module.__class__.__name__ == "Linear" or child_module.__class__.__name__ == "LoRACompatibleLinear":
                        lora_module = LoConModule(lora_name, child_module, self.multiplier, self.r, alpha, dropout)
                    elif child_module.__class__.__name__ == "Conv2d" or child_module.__class__.__name__ == "LoRACompatibleConv":
                        k_size, *_ = child_module.kernel_size
                        if k_size == 1:
                            lora_module = LoConModule(lora_name, child_module, self.multiplier, self.r, alpha, dropout)
                        else:
                            lora_module = LoConModule(lora_name, child_module, self.multiplier, self.conv_r, self.conv_alpha, dropout)
                    else:
                        continue
                    blocks.append(lora_module)
            elif name in target_replace_module:
                lora_name = prefix + '.' + name 
                lora_name = lora_name.replace('.', '_')
                if module.__class__.__name__ == "Linear" or child_module.__class__.__name__ == "LoRACompatibleLinear":
                    lora_module = LoConModule(lora_name, module, self.multiplier, self.r, alpha, dropout)
                elif module.__class__.__name__ == "Conv2d" or child_module.__class__.__name__ == "LoRACompatibleConv":
                    k_size, *_ = module.kernel_size
                    if k_size == 1:
                        lora_module = LoConModule(lora_name, module, self.multiplier, self.r, alpha, dropout)
                    else:
                        lora_module = LoConModule(lora_name, module, self.multiplier, self.conv_r, self.conv_alpha, dropout)
                else:
                    continue
                blocks.append(lora_module)
                    
        return blocks
    
    def inject(self, unet=True, text_encoder=True):
        if not unet:
            self.unet_loras = []
        if not text_encoder:
            self.text_encoder_loras = []
            
        for lora in self.text_encoder_loras + self.unet_loras:
            lora.inject()
            self.add_module(lora.lora_name, lora)
            

class LoConModule(torch.nn.Module):
    
    def __init__(self, lora_name, base_layer, multiplier=1.0, lora_dim=4, alpha=1, dropout=0):
        super().__init__()
        self.lora_name = lora_name    
        self.lora_dim = lora_dim
        self.dropout = nn.Dropout(dropout) if dropout else nn.Identity()
        
        if base_layer.__class__.__name__ in ['Conv2d', 'LoRACompatibleConv']:
            in_dim = base_layer.in_channels
            k_size = base_layer.kernel_size
            stride = base_layer.stride
            padding = base_layer.padding
            out_dim = base_layer.out_channels
            self.lora_down = nn.Conv2d(in_dim, lora_dim, k_size, stride, padding, bias=False)
            self.lora_up = nn.Conv2d(lora_dim, out_dim, (1, 1), bias=False)
        else:
            in_dim = base_layer.in_features
            out_dim = base_layer.out_features
            self.lora_down = torch.nn.Linear(in_dim, lora_dim, bias=False)
            self.lora_up = torch.nn.Linear(lora_dim, out_dim, bias=False)
        
        self.multiplier = multiplier
        self.base_layer = base_layer
        
        alpha = lora_dim if alpha is None or alpha == 0 else alpha
        self.scale = alpha / self.lora_dim
        self.register_buffer('alpha', torch.tensor(alpha))   
        
        torch.nn.init.kaiming_uniform_(self.lora_down.weight, a=math.sqrt(5))
        torch.nn.init.zeros_(self.lora_up.weight)
    
    def inject(self):
        # inject and init weights
        self.org_forward = self.base_layer.forward
        self.base_layer.forward = self.forward
        del self.base_layer

    @torch.enable_grad() 
    def forward(self, x):
        return self.org_forward(x) + self.dropout(self.lora_up(self.lora_down(x))) * self.multiplier * self.scale


class LoConDiffusionModel(StableDiffusionModel):
    def __init__(self, *args):
        super().__init__(*args)
        
    def init_model(self):
        super().init_model()
            
        self.unet.train()
        self.unet.requires_grad_(False)
        
        if self.config.lora.train_text_encoder and not self.is_sdxl:
            self.text_encoder.train()
        
        if hasattr(self, "text_encoder"):
            self.text_encoder.requires_grad_(False)
            self.text_encoder.text_model.embeddings.requires_grad_(True)
            if self.config.trainer.gradient_checkpointing:
                self.text_encoder.gradient_checkpointing_enable()
            
        self.lora = LoConBaseModel(self.unet, getattr(self, "text_encoder", None), self.config.lora)
        self.lora.inject(self.config.lora.train_unet, self.config.lora.train_text_encoder)
        self.lora.requires_grad_(True)

    def on_train_epoch_start(self):
        super().on_train_epoch_start()
        
    def on_save_checkpoint(self, checkpoint):
        checkpoint["state_dict"] = {k: v for k, v in checkpoint["state_dict"].items() if k.startswith("lora.")}

    def encode_tokens(self, prompts, tokenizer=None):
        with torch.set_grad_enabled(self.config.lora.train_text_encoder):
            return super().encode_tokens(prompts, tokenizer)
        
    def configure_optimizers(self):
        enumerate_params = lambda loras: itertools.chain.from_iterable([lora.parameters() for lora in loras])
        params_to_optim = []
        if self.config.lora.train_unet:
            new_unet_lr, scaled = self.get_scaled_lr(self.config.lora.unet_lr)
            if scaled:
                print(f"Using scaled unet LR (LoRA): {new_unet_lr}")
            params_to_optim.append({
                'params': enumerate_params(self.lora.unet_loras),
                'lr': new_unet_lr
            })
                
        if self.config.lora.train_text_encoder:
            new_encoder_lr, scaled = self.get_scaled_lr(self.config.lora.encoder_lr)
            if scaled:
                print(f"Using scaled text_encoder LR (LoRA): {new_encoder_lr}")
            params_to_optim.append({
                'params': enumerate_params(self.lora.text_encoder_loras),
                'lr': new_encoder_lr
            })
        
        optimizer = get_class(self.config.optimizer.name)(
            params_to_optim, 
            **self.config.optimizer.params
        )
        scheduler = get_class(self.config.lr_scheduler.name)(
            optimizer=optimizer,
            **self.config.lr_scheduler.params
        )
        return [[optimizer], [scheduler]]
    
