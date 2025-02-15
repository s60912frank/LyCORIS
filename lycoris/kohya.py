# network module for kohya
# reference:
# https://github.com/microsoft/LoRA/blob/main/loralib/layers.py
# https://github.com/cloneofsimo/lora/blob/master/lora_diffusion/lora.py
# https://github.com/kohya-ss/sd-scripts/blob/main/networks/lora.py

import math
import os
from typing import List
import torch

from .kohya_utils import *
from .locon import LoConModule
from .loha import LohaModule


def create_network(multiplier, network_dim, network_alpha, vae, text_encoder, unet, **kwargs):
    if network_dim is None:
        network_dim = 4                     # default
    conv_dim = int(kwargs.get('conv_dim', network_dim))
    conv_alpha = float(kwargs.get('conv_alpha', network_alpha))
    dropout = float(kwargs.get('dropout', 0.))
    algo = kwargs.get('algo', 'lora')
    network_module = {
        'lora': LoConModule,
        'loha': LohaModule,
    }[algo]
    
    print(f'Using rank adaptation algo: {algo}')
    network = LoRANetwork(
        text_encoder, unet, 
        multiplier=multiplier, 
        lora_dim=network_dim, conv_lora_dim=conv_dim, 
        alpha=network_alpha, conv_alpha=conv_alpha,
        dropout=dropout,
        network_module=network_module
    )
    
    return network


def create_network_from_weights(multiplier, file, vae, text_encoder, unet, **kwargs):
    if os.path.splitext(file)[1] == '.safetensors':
        from safetensors.torch import load_file, safe_open
        weights_sd = load_file(file)
    else:
        weights_sd = torch.load(file, map_location='cpu')

    # get dim (rank)
    network_alpha = None
    network_dim = None
    for key, value in weights_sd.items():
        if network_alpha is None and 'alpha' in key:
            network_alpha = value
        if network_dim is None and 'lora_down' in key and len(value.size()) == 2:
            network_dim = value.size()[0]

    if network_alpha is None:
        network_alpha = network_dim

    network = LoRANetwork(text_encoder, unet, multiplier=multiplier, lora_dim=network_dim, alpha=network_alpha)
    network.weights_sd = weights_sd
    return network


class LoRANetwork(torch.nn.Module):
    '''
    LoRA + LoCon
    '''
    # Ignore proj_in or proj_out, their channels is only a few.
    UNET_TARGET_REPLACE_MODULE = [
        "Transformer2DModel", 
        "Attention", 
        "ResnetBlock2D", 
        "Downsample2D", 
        "Upsample2D"
    ]
    TEXT_ENCODER_TARGET_REPLACE_MODULE = ["CLIPAttention", "CLIPMLP"]
    LORA_PREFIX_UNET = 'lora_unet'
    LORA_PREFIX_TEXT_ENCODER = 'lora_te'

    def __init__(
        self, 
        text_encoder, unet, 
        multiplier=1.0, 
        lora_dim=4, conv_lora_dim=4, 
        alpha=1, conv_alpha=1,
        dropout = 0, network_module = LoConModule,
    ) -> None:
        super().__init__()
        self.multiplier = multiplier
        self.lora_dim = lora_dim
        self.conv_lora_dim = int(conv_lora_dim)
        if self.conv_lora_dim != self.lora_dim: 
            print('Apply different lora dim for conv layer')
            print(f'LoCon Dim: {conv_lora_dim}, LoRA Dim: {lora_dim}')
            
        self.alpha = alpha
        self.conv_alpha = float(conv_alpha)
        if self.alpha != self.conv_alpha: 
            print('Apply different alpha value for conv layer')
            print(f'LoCon alpha: {conv_alpha}, LoRA alpha: {alpha}')
        
        if 1 >= dropout >= 0:
            print(f'Use Dropout value: {dropout}')
        self.dropout = dropout
        
        # create module instances
        def create_modules(prefix, root_module: torch.nn.Module, target_replace_modules) -> List[network_module]:
            print('Create LoCon Module')
            loras = []
            for name, module in root_module.named_modules():
                if module.__class__.__name__ in target_replace_modules:
                    for child_name, child_module in module.named_modules():
                        lora_name = prefix + '.' + name + '.' + child_name
                        lora_name = lora_name.replace('.', '_')
                        if child_module.__class__.__name__ == 'Linear' and lora_dim>0:
                            lora = network_module(
                                lora_name, child_module, self.multiplier, 
                                self.lora_dim, self.alpha, self.dropout
                            )
                        elif child_module.__class__.__name__ == 'Conv2d':
                            k_size, *_ = child_module.kernel_size
                            if k_size==1 and lora_dim>0:
                                lora = network_module(
                                    lora_name, child_module, self.multiplier, 
                                    self.lora_dim, self.alpha, self.dropout
                                )
                            elif conv_lora_dim>0:
                                lora = network_module(
                                    lora_name, child_module, self.multiplier, 
                                    self.conv_lora_dim, self.conv_alpha, self.dropout
                                )
                            else:
                                continue
                        else:
                            continue
                        loras.append(lora)
            return loras

        self.text_encoder_loras = create_modules(
            LoRANetwork.LORA_PREFIX_TEXT_ENCODER,
            text_encoder, 
            LoRANetwork.TEXT_ENCODER_TARGET_REPLACE_MODULE
        )
        print(f"create LoRA for Text Encoder: {len(self.text_encoder_loras)} modules.")

        self.unet_loras = create_modules(LoRANetwork.LORA_PREFIX_UNET, unet, LoRANetwork.UNET_TARGET_REPLACE_MODULE)
        print(f"create LoRA for U-Net: {len(self.unet_loras)} modules.")

        self.weights_sd = None

        # assertion
        names = set()
        for lora in self.text_encoder_loras + self.unet_loras:
            assert lora.lora_name not in names, f"duplicated lora name: {lora.lora_name}"
            names.add(lora.lora_name)

    def set_multiplier(self, multiplier):
        self.multiplier = multiplier
        for lora in self.text_encoder_loras + self.unet_loras:
            lora.multiplier = self.multiplier
            
    def load_weights(self, file):
        if os.path.splitext(file)[1] == '.safetensors':
            from safetensors.torch import load_file, safe_open
            self.weights_sd = load_file(file)
        else:
            self.weights_sd = torch.load(file, map_location='cpu')

    def apply_to(self, text_encoder, unet, apply_text_encoder=None, apply_unet=None):
        if self.weights_sd:
            weights_has_text_encoder = weights_has_unet = False
            for key in self.weights_sd.keys():
                if key.startswith(LoRANetwork.LORA_PREFIX_TEXT_ENCODER):
                    weights_has_text_encoder = True
                elif key.startswith(LoRANetwork.LORA_PREFIX_UNET):
                    weights_has_unet = True

            if apply_text_encoder is None:
                apply_text_encoder = weights_has_text_encoder
            else:
                assert apply_text_encoder == weights_has_text_encoder, f"text encoder weights: {weights_has_text_encoder} but text encoder flag: {apply_text_encoder} / 重みとText Encoderのフラグが矛盾しています"

            if apply_unet is None:
                apply_unet = weights_has_unet
            else:
                assert apply_unet == weights_has_unet, f"u-net weights: {weights_has_unet} but u-net flag: {apply_unet} / 重みとU-Netのフラグが矛盾しています"
        else:
            assert apply_text_encoder is not None and apply_unet is not None, f"internal error: flag not set"

        if apply_text_encoder:
            print("enable LoRA for text encoder")
        else:
            self.text_encoder_loras = []

        if apply_unet:
            print("enable LoRA for U-Net")
        else:
            self.unet_loras = []

        for lora in self.text_encoder_loras + self.unet_loras:
            lora.apply_to()
            self.add_module(lora.lora_name, lora)

        if self.weights_sd:
            # if some weights are not in state dict, it is ok because initial LoRA does nothing (lora_up is initialized by zeros)
            info = self.load_state_dict(self.weights_sd, False)
            print(f"weights are loaded: {info}")

    def enable_gradient_checkpointing(self):
        # not supported
        def make_ckpt(module):
            if isinstance(module, torch.nn.Module):
                module.grad_ckpt = True
        self.apply(make_ckpt)
        pass

    def prepare_optimizer_params(self, text_encoder_lr, unet_lr):
        def enumerate_params(loras):
            params = []
            for lora in loras:
                params.extend(lora.parameters())
            return params

        self.requires_grad_(True)
        all_params = []

        if self.text_encoder_loras:
            param_data = {'params': enumerate_params(self.text_encoder_loras)}
            if text_encoder_lr is not None:
                param_data['lr'] = text_encoder_lr
            all_params.append(param_data)

        if self.unet_loras:
            param_data = {'params': enumerate_params(self.unet_loras)}
            if unet_lr is not None:
                param_data['lr'] = unet_lr
            all_params.append(param_data)

        return all_params

    def prepare_grad_etc(self, text_encoder, unet):
        self.requires_grad_(True)

    def on_epoch_start(self, text_encoder, unet):
        self.train()

    def get_trainable_params(self):
        return self.parameters()

    def save_weights(self, file, dtype, metadata):
        if metadata is not None and len(metadata) == 0:
            metadata = None

        state_dict = self.state_dict()

        if dtype is not None:
            for key in list(state_dict.keys()):
                v = state_dict[key]
                v = v.detach().clone().to("cpu").to(dtype)
                state_dict[key] = v

        if os.path.splitext(file)[1] == '.safetensors':
            from safetensors.torch import save_file

            # Precalculate model hashes to save time on indexing
            if metadata is None:
                metadata = {}
            model_hash, legacy_hash = precalculate_safetensors_hashes(state_dict, metadata)
            metadata["sshs_model_hash"] = model_hash
            metadata["sshs_legacy_hash"] = legacy_hash

            save_file(state_dict, file, metadata)
        else:
            torch.save(state_dict, file)
