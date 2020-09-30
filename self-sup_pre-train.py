#  Copyright (c) 2020
#  Licensed under The MIT License
#  Written by Zhiheng Li
#  Email: zhiheng.li@rochester.edu

import os
import torch
import torch.optim as optim
import tqdm
import itertools
import shutil

from option import arg_parse
import dataset
from torch_geometric.data import DataLoader
from model.networks import DSGPM
from utils.util import get_run_name, save_args
from torch.utils.tensorboard import SummaryWriter

from utils.stat import AverageMeter
from utils.transforms import MaskAtomType
from sklearn.metrics import precision_score

from warnings import simplefilter
from sklearn.exceptions import UndefinedMetricWarning
simplefilter(action='ignore', category=FutureWarning)
simplefilter(action='ignore', category=UndefinedMetricWarning)


class Trainer:
    def __init__(self, args):
        self.args = args

        if not args.debug:
            run_name = get_run_name(args.title)

            self.ckpt_dir = os.path.join(args.ckpt, run_name)
            if not os.path.exists(self.ckpt_dir):
                os.makedirs(self.ckpt_dir)

            save_args(args, self.ckpt_dir)

            if args.tb_log:
                tensorboard_dir = os.path.join(args.tb_root, run_name)
                if not os.path.exists(tensorboard_dir):
                    os.mkdir(tensorboard_dir)

                self.writer = SummaryWriter(tensorboard_dir)

        assert args.split_index_folder is not None
        dataset_class = dataset.get_dataset_class(args.dataset)
        dataset_args = {'data_root': args.data_root, 'split_index_folder': args.split_index_folder,
                        'cycle_feat': args.use_cycle_feat, 'degree_feat': args.use_degree_feat,
                        'transform': MaskAtomType(args.mask_ratio,
                                                  weight=dataset_class.compute_cls_weight()
                                                  if args.weighted_sample_mask else None),
                        'sample_ratio': args.sample_ratio}
        train_set = dataset_class(split='train', **dataset_args)
        val_set = dataset_class(split='val', **dataset_args)

        dataloader_args = {'batch_size': args.batch_size, 'num_workers': args.num_workers, 'pin_memory': True}
        self.train_loader = DataLoader(train_set, **dataloader_args)
        self.val_loader = DataLoader(val_set, **dataloader_args)

        self.model = DSGPM(args.num_atoms, args.hidden_dim,
                      args.output_dim, args=args).cuda()
        final_feat_dim = args.output_dim + args.num_atoms + 1
        if self.args.use_cycle_feat:
            final_feat_dim += 1
        if self.args.use_degree_feat:
            final_feat_dim += 1
        self.atom_type_classifier = torch.nn.Linear(final_feat_dim, args.num_atoms).cuda()
        self.criterion = torch.nn.CrossEntropyLoss(weight=dataset_class.compute_cls_weight() if args.weighted_ce else None)

        # setup optimizer
        self.optimizer = optim.Adam(itertools.chain(self.model.parameters(),
                                                    self.atom_type_classifier.parameters()),
                                    lr=args.lr, weight_decay=args.weight_decay)

        self.best_acc = -1

    def train(self, epoch):
        self.model.train()
        loss_meter = AverageMeter()
        accuracy_meter = AverageMeter()

        train_loader = iter(self.train_loader)

        tbar = tqdm.tqdm(enumerate(train_loader), total=len(self.train_loader), dynamic_ncols=True)

        for i, data in tbar:
            data = data.to(torch.device(0))
            self.optimizer.zero_grad()

            fg_embed = self.model(data)
            pred = self.atom_type_classifier(fg_embed[data.masked_atom_index])
            loss = self.criterion(pred, data.masked_atom_type)
            loss.backward()
            self.optimizer.step()

            loss_meter.update(loss.item())
            accuracy = precision_score(data.masked_atom_type.cpu().numpy(),
                                       torch.max(pred.detach(), dim=1)[1].cpu().numpy(),
                                       labels=range(self.args.num_atoms), average='macro')
            accuracy_meter.update(accuracy)

            tbar.set_description('[%d/%d] loss: %.4f, accuracy: %.4f'
                                 % (epoch, self.args.epoch, loss_meter.avg, accuracy_meter.avg))

        if not self.args.debug and self.args.tb_log:
            self.writer.add_scalar('loss', loss_meter.avg, epoch)
            self.writer.add_scalar('train_accuracy', accuracy_meter.avg, epoch)

        if not self.args.debug:
            state_dict = self.model.module.state_dict() if not isinstance(self.model, DSGPM) else self.model.state_dict()
            torch.save(state_dict, os.path.join(self.ckpt_dir, '{}.pth'.format(epoch)))

    def eval(self, epoch):
        is_best = False
        self.model.eval()
        accuracy_meter = AverageMeter()

        val_loader = iter(self.val_loader)
        tbar = tqdm.tqdm(enumerate(val_loader), total=len(self.val_loader), dynamic_ncols=True)
        for i, data in tbar:
            data = data.to(torch.device(0))

            fg_embed = self.model(data)
            pred = self.atom_type_classifier(fg_embed[data.masked_atom_index])

            # accuracy = float(torch.sum(torch.max(pred.detach(), dim=1)[1] == data.masked_atom_type).cpu().item()) / len(pred)

            # use macro to compute unweighted average of precision per class
            accuracy = precision_score(data.masked_atom_type.cpu().numpy(), torch.max(pred.detach(), dim=1)[1].cpu().numpy(),
                                       labels=range(self.args.num_atoms), average='macro')
            accuracy_meter.update(accuracy)

            tbar.set_description('[%d/%d] accuracy: %.4f'
                                 % (epoch, self.args.epoch, accuracy_meter.avg))

        if not self.args.debug and self.args.tb_log:
            self.writer.add_scalar('val_accuracy', accuracy_meter.avg, epoch)

        if accuracy_meter.avg > self.best_acc:
            is_best = True
            self.best_acc = accuracy_meter.avg

        if not self.args.debug:
            state_dict = self.model.module.state_dict() if not isinstance(self.model, DSGPM) else self.model.state_dict()
            ckpt_fpath = os.path.join(self.ckpt_dir, '{}.pth'.format(epoch))
            torch.save(state_dict, ckpt_fpath)
            if is_best:
                shutil.copyfile(ckpt_fpath, os.path.join(self.ckpt_dir, 'best.pth'))


def main():
    args = arg_parse()
    args.use_mask_embed = True
    assert args.ckpt is not None, '--ckpt is required'

    args.devices = [int(device_id) for device_id in args.devices.split(',')]

    trainer = Trainer(args)

    for e in range(1, args.epoch + 1):
        trainer.train(e)
        with torch.no_grad():
            trainer.eval(e)


if __name__ == '__main__':
    main()
