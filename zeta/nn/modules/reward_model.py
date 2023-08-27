import copy
from pathlib import Path

import torch
import torch.nn.functional as F
from beartype import beatype
from einops import rearrange, repeat
from einops.layers.torch import Rearrange
from torch import nn


def exists(val):
    return val is not None

@beatype
class RewardModel(nn.Module):
    def __init__(
            self,
            model,
            dropout = 0.1,
            num_binned_output = 0.,
            use_lora=True,
            lora_r=8,
            reward_lora_scope="reward",
    ):
        super().__init__()

        self.model = copy.deepcopy(model)
        self.model.set_dropout(dropout)

        self.reward_lora_scope = reward_lora_scope if use_lora else None

        if exists(self.reward_lora_scope):
            self.model.add_finetune_params(reward_lora_scope, lora_r=lora_r)
        
        dim = model.dim
        
        self.binned_output = num_binned_output > 1

        self.prompt_embed = nn.Parameter(torch.zeros(1, 1, dim))
        self.response_embed = nn.Parameter(torch.zeros(1, 1, dim))

        if self.binned_output:
            self.to_pred = nn.Linear(dim, num_binned_output)
        
        else:
            self.to_pred = nn.Sequential(
                nn.Linear(dim, 1, bias=False),
                Rearrange('... 1 -> ...')
            )
    
    def load(self, path):
        path = Path(path)
        assert path.exists()
        self.load_state_dict(torch.load(path))
    
    def finetune_parameters(self):
        return [
            *self.to_pred.parameters(),
            *(self.model.finetune_parameters(self.reward_lora_scope) \
              if exists(self.reward_lora_scope) else self.model.parameters())
        ]
    
    def forward(
            self,
            x,
            mask=None,
            prompt_mask=None,
            prompt_lengths=None,
            labels=None,
            sample=None,
            sample_temperature=1.,
            disable_lora=False
    ):
        assert not(exists(prompt_mask) and exists(prompt_lengths))

        if exists(prompt_lengths):
            batch, seq_len = x.shape
            arange = torch.arange(seq_len, device=x.device)
            prompt_mask = repeat(arange, 'n -> b n', b=batch) < rearrange(prompt_lengths, 'b -> b 1')
        
        #model need to know what is prompt and what is response

        extra_embed=None

        if exists(prompt_mask):
            extra_embed = torch.where(
                rearrange(prompt_mask, 'b n -> b n 1'),
                self.prompt_embed,
                self.response_embed
            )
        
        embeds = self.model(
            x,
            extra_embed=extra_embed,
            return_only_embedding=True,
            disable_lora=disable_lora,
            finetune_scope=self.reward_lora_scope
        )

        pooled = masked_mean(embeds, mask, dim=1)
        pred = self.to_pred(pooled)

        if sample and self.binned_output:
            assert not exists(labels)
            pred = gumbel_sample(pred, temperature = sample_temperature, dim=-1)

        if not exists(labels):
            return pred
        
        if not self.binned_output:
            return F.mse_loss(pred, labels)
        
        if not self.binned_output:
            return F.mse_loss(pred, labels)
        
        return F.cross_entropy(pred, labels)
