from PIL import Image
import numpy as np
import torch
import torch.nn as nn
import torchvision.transforms as transforms
import matplotlib.pyplot as plt
import cv2
import sys
from sklearn.neighbors import NearestNeighbors
from sklearn.decomposition import PCA
import matplotlib.pyplot as plt
from matplotlib.patches import ConnectionPatch
import numpy as np
from torch.utils.data import Dataset, DataLoader
import os
import random
from utils import *


class ModifiedAttention(nn.Module):
    def __init__(self, original_attn):
        super(ModifiedAttention, self).__init__()
        self.qkv = original_attn.qkv
        self.attn_drop = original_attn.attn_drop
        self.proj = original_attn.proj
        self.proj_drop = original_attn.proj_drop
        self.num_heads = original_attn.num_heads
        self.scale = original_attn.scale

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn_weights = attn.clone()
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x, attn_weights

class ModifiedNestedTensorBlock(nn.Module):
    def __init__(self, original_block):
        super(ModifiedNestedTensorBlock, self).__init__()
        self.norm1 = original_block.norm1
        self.attn = ModifiedAttention(original_block.attn)
        self.ls1 = original_block.ls1
        self.drop_path1 = original_block.drop_path1
        self.norm2 = original_block.norm2
        self.mlp = original_block.mlp
        self.ls2 = original_block.ls2
        self.drop_path2 = original_block.drop_path2

    def forward(self, x, return_attention=False):
        if return_attention:
            x_norm = self.norm1(x)
            attn_output, attn_weights = self.attn(x_norm)
            x = x + self.drop_path1(self.ls1(attn_output))
            x = x + self.drop_path2(self.ls2(self.mlp(self.norm2(x))))
            return x, attn_weights
        else:
            x = x + self.drop_path1(self.ls1(self.attn(self.norm1(x))[0]))
            x = x + self.drop_path2(self.ls2(self.mlp(self.norm2(x))))
            return x
        
class SingleHeadAttention(nn.Module):
    def __init__(self, embed_dim):
        super(SingleHeadAttention, self).__init__()
        self.qkv = nn.Linear(embed_dim, embed_dim * 3)
        self.scale = (embed_dim // 1) ** -0.5  # Single head
        self.attn_drop = nn.Dropout(0.1)
        self.proj = nn.Linear(embed_dim, embed_dim)
        self.proj_drop = nn.Dropout(0.1)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, 1, C // 1).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn_weights = attn.clone()
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x, attn_weights

class CroDINO(nn.Module):
    def __init__(self, repo_name="facebookresearch/dinov2", model_name="dinov2_vitb14", pretrained=True):
        super(CroDINO, self).__init__()
        original_model = torch.hub.load(repo_name, model_name)
        modified_blocks = nn.ModuleList([ModifiedNestedTensorBlock(blk) for blk in original_model.blocks])
        
        self.patch_embed = original_model.patch_embed
        self.blocks = modified_blocks
        self.norm = original_model.norm
        self.head = original_model.head
        
        # Final single-head attention layer
        embed_dim = original_model.patch_embed.proj.out_channels
        self.final_attention = SingleHeadAttention(embed_dim)

        # Positional Encoding
        self.pos_embed = nn.Parameter(original_model.pos_embed)  # use the original positional embedding and adjust dynamically
        # self.pos_embed = nn.Parameter(torch.cat([original_model.pos_embed[:, :num_patches], original_model.pos_embed[:, :num_patches]], dim=1))
        # self.pos_embed = nn.Parameter(self.pos_embed[:, :512, :])  # 512 = 256 patches per image * 2 images

        # Freeze parameters if pretrained is True
        if pretrained:
            for param in self.patch_embed.parameters():
                param.requires_grad = False
            for param in self.blocks.parameters():
                param.requires_grad = False
            for param in self.norm.parameters():
                param.requires_grad = False
            for param in self.head.parameters():
                param.requires_grad = False

    def forward(self, x1, x2, return_attention=False):
        x1 = self.patch_embed(x1)
        x2 = self.patch_embed(x2)
        
        # Compute positional encodings dynamically
        num_patches_x1 = x1.size(1)
        num_patches_x2 = x2.size(1)
        pos_embed_x1 = self.pos_embed[:, :num_patches_x1, :]
        pos_embed_x2 = self.pos_embed[:, :num_patches_x2, :]
        
        # Concatenate tokens from both images
        x = torch.cat((x1, x2), dim=1)
        
        # Add positional encoding
        pos_embed = torch.cat((pos_embed_x1, pos_embed_x2), dim=1)
        x = x + pos_embed
        
        # Collect attention weights if needed
        attention_weights = []
        
        # Process through transformer blocks
        for blk in self.blocks:
            if return_attention:
                x, attn = blk(x, return_attention=True)
                attention_weights.append(attn)
            else:
                x = blk(x)
        
        # Final single-head attention
        _, final_attn = self.final_attention(x)         # NOTE: I'm computing the attention without affecting the patches
        if return_attention:
            attention_weights.append(final_attn)
        
        x = self.norm(x)
        x = self.head(x)
        
        if return_attention:
            return x, attention_weights
        else:
            return x
        

def get_patch_embeddings(model, x):
    x = model.patch_embed(x)
    for blk in model.blocks:
        x = blk(x)
    x = model.norm(x)
    return x