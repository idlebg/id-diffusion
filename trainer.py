# torchrun trainer.py --model_path=/tmp/model --config test-run.yaml

from functools import partial
import pytorch_lightning as pl
import torch

from data.buckets import init_sampler
from data.store import AspectRatioDataset
from lib.args import parse_args
from lib.model import load_model
from lib.utils import get_world_size

from omegaconf import OmegaConf
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.strategies import HivemindStrategy
from hivemind import Float16Compression

torch.backends.cudnn.benchmark = True

args = parse_args()
config = OmegaConf.load(args.config)
torch.cuda.set_device(args.local_rank)
device = torch.device("cuda")
world_size = get_world_size()
weight_dtype = torch.float16 if config.trainer.precision == "fp16" else torch.float32


def main(args):
    torch.manual_seed(config.trainer.seed)
    tokenizer, model = load_model(args.model_path, config)
    dataset = AspectRatioDataset(
        tokenizer=tokenizer,
        size=config.trainer.resolution,
        bsz=args.train_batch_size,
        seed=config.trainer.seed,
        **config.dataset
    )
    
    train_dataloader = torch.utils.data.DataLoader(
        dataset,
        collate_fn=dataset.collate_fn,
        sampler=init_sampler(
            args, config=config, dataset=dataset, world_size=world_size
        ),
        num_workers=8,
    )
    
    logger = (
        WandbLogger(project=config.monitor.wandb_id)
        if config.monitor.wandb_id != ""
        else None
    )
    
    hivemind = (
        HivemindStrategy(
            scheduler_fn=partial(
                torch.optim.lr_scheduler.CosineAnnealingWarmRestarts, T_0=1000
            ),
            grad_compression=Float16Compression(),
            state_averaging_compression=Float16Compression(),
            **config.hivemind
        )
        if config.trainer.use_hivemind
        else None
    )
    
    trainer = pl.Trainer(
        limit_train_batches=100,
        max_epochs=config.trainer.max_train_epoch,
        accelerator="gpu",
        logger=logger,
        strategy=hivemind,
    )
    trainer.fit(model=model, train_dataloaders=train_dataloader)


if __name__ == "__main__":
    args = parse_args()
    main(args)
