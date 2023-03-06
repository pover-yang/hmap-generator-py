import cv2
import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
# from post_precess import blend_hmap_img
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint

from model.unet import UNet
from utils.lr_utils import warmup_lr
from utils.misc import blend_image_hmap_tensor


def sigmoid_focal_loss(preds, targets, alpha=0.25, gamma=2, reduction="mean") -> torch.Tensor:
    preds = torch.sigmoid(preds)
    ce_loss = F.binary_cross_entropy_with_logits(preds, targets, reduction="none")
    p_t = preds * targets + (1 - preds) * (1 - targets)
    loss = ce_loss * ((1 - p_t) ** gamma)

    if alpha >= 0:
        alpha_t = alpha * targets + (1 - alpha) * (1 - targets)
        loss = alpha_t * loss

    # Check reduction option and return loss accordingly
    if reduction == "none":
        pass
    elif reduction == "mean":
        loss = loss.mean()
    elif reduction == "sum":
        loss = loss.sum()
    else:
        raise ValueError(
            f"Invalid Value for arg 'reduction': '{reduction} \n Supported reduction modes: 'none', 'mean', 'sum'"
        )
    return loss


class FocalLoss(nn.Module):
    def __init__(self, gamma=2.0, alpha=1.0, reduction="mean"):
        super(FocalLoss, self).__init__()
        self.__gamma = gamma
        self.__alpha = alpha
        self.__loss = nn.BCEWithLogitsLoss(reduction=reduction)

    def forward(self, pred, target):
        loss = self.__loss(input=pred, target=target)
        loss *= self.__alpha * torch.pow(torch.abs(target - torch.sigmoid(pred)), self.__gamma)  # gamma相当于Heatmap的alpha
        return loss


class LitUNet(UNet, pl.LightningModule):
    def __init__(self, model_conf, sample_loader=None):
        super().__init__(**vars(model_conf))
        self.loss = FocalLoss()
        self.sample_loader = sample_loader
        self.init_lr = model_conf.init_lr

    def training_step(self, batch, batch_idx):
        images, targets = batch
        preds = self.forward(images)
        loss = sigmoid_focal_loss(preds, targets)
        self.log('train_loss', loss, on_step=False, on_epoch=True, logger=True, sync_dist=True)
        return loss

    def validation_step(self, batch, batch_idx):
        images, targets = batch
        preds = self.forward(images)
        loss = sigmoid_focal_loss(preds, targets)
        self.log('val_loss', loss, on_step=False, on_epoch=True, logger=True, sync_dist=True)
        return loss

    def on_validation_end(self):
        if self.sample_loader is None:
            return
        images, targets = next(iter(self.sample_loader))
        images = images.to(self.device)
        preds = self.forward(images)
        img_with_hmap = blend_image_hmap_tensor(images, preds, alpha=0.3)
        self.logger.experiment.add_image(f'image with hmap', img_with_hmap, self.current_epoch)

        save_img = (img_with_hmap.detach().cpu().numpy().transpose((1, 2, 0)) * 255).astype('uint8')
        save_img = cv2.cvtColor(save_img, cv2.COLOR_RGB2BGR)
        cv2.imwrite(f'{self.logger.log_dir}/{self.current_epoch}.png', save_img)

    def configure_optimizers(self):
        lr_lambda = warmup_lr(max_epochs=self.trainer.max_epochs, warmup_epochs=5)
        optimizer = torch.optim.Adam(self.parameters(), lr=self.init_lr)
        lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)
        return [optimizer], [lr_scheduler]

    def configure_callbacks(self):
        lr_monitor = LearningRateMonitor(logging_interval='epoch')

        model_checkpoint = ModelCheckpoint(
            monitor='val_loss',
            filename='hmap_{epoch:03d}_{val_loss:.4f}',
            save_top_k=50,
            mode='min',
        )

        return [lr_monitor, model_checkpoint]


if __name__ == '__main__':
    from thop import profile

    from dataset.hamp_ds import HeatMapDataset

    model = UNet(1, 3)
    dataset = HeatMapDataset('/home/pover/Datasets/barcode', mode='sample')
    in_tensor = dataset[0][0].unsqueeze(0)

    out_tensor = model(in_tensor)
    print('out_tensor.shape: ', out_tensor.shape)

    flops, params = profile(model, inputs=(in_tensor,))
    print(f"Flops: {flops / 1e9:.3f}G, Params: {params / 1e6:.3f}M")
