import torch
import torch.nn as nn
from torchvision.models.detection.faster_rcnn import FasterRCNN
from torchvision.models.detection.backbone_utils import BackboneWithFPN
from torchvision.models import resnet
from torchvision.ops import misc as misc_nn_ops

from rastervision.backend.torch_utils.boxlist import BoxList


def get_out_channels(model):
    out = {}

    def make_save_output(layer_name):
        def save_output(layer, input, output):
            out[layer_name] = output.shape[1]

        return save_output

    model.layer1.register_forward_hook(make_save_output('layer1'))
    model.layer2.register_forward_hook(make_save_output('layer2'))
    model.layer3.register_forward_hook(make_save_output('layer3'))
    model.layer4.register_forward_hook(make_save_output('layer4'))

    model(torch.empty((1, 3, 128, 128)))
    return [out['layer1'], out['layer2'], out['layer3'], out['layer4']]


# This fixes a bug in torchvision.
def resnet_fpn_backbone(backbone_name, pretrained):
    backbone = resnet.__dict__[backbone_name](
        pretrained=pretrained, norm_layer=misc_nn_ops.FrozenBatchNorm2d)

    # freeze layers
    for name, parameter in backbone.named_parameters():
        if 'layer2' not in name and 'layer3' not in name and 'layer4' not in name:
            parameter.requires_grad_(False)

    return_layers = {'layer1': 0, 'layer2': 1, 'layer3': 2, 'layer4': 3}

    out_channels = 256
    in_channels_list = get_out_channels(backbone)
    return BackboneWithFPN(backbone, return_layers, in_channels_list,
                           out_channels)


class MyFasterRCNN(nn.Module):
    """Adapter around torchvision Faster-RCNN.

    The purpose of the adapter is to use a different input and output format
    and inject bogus boxes to circumvent torchvision's inability to handle
    training examples with no ground truth boxes.
    """

    def __init__(self, backbone_arch, num_labels, img_sz, pretrained=True):
        super().__init__()

        backbone = resnet_fpn_backbone(backbone_arch, pretrained)
        self.model = FasterRCNN(
            backbone, num_labels, min_size=img_sz, max_size=img_sz)
        self.subloss_names = [
            'total_loss', 'loss_box_reg', 'loss_classifier', 'loss_objectness',
            'loss_rpn_box_reg'
        ]

    def forward(self, input, targets=None):
        """Forward pass

        Args:
            input: tensor<n, 3, h, w> with batch of images
            targets: None or list<BoxList> of length n with boxes and labels

        Returns:
            if targets is None, returns list<BoxList> of length n, containing
            boxes, labels, and scores for boxes with score > 0.05. Further
            filtering based on score should be done before considering the
            prediction "final".

            if targets is a list, returns the losses as dict with keys from
            self.subloss_names.
        """
        if targets:
            # Add bogus background class box for each image to workaround
            # the inability of torchvision to train on images with
            # no ground truth boxes. This is important for being able
            # to handle negative chips generated by RV.
            new_targets = []
            for x, y in zip(input, targets):
                h, w = x.shape[1:]
                boxes = torch.cat(
                    [
                        y.boxes,
                        torch.tensor([[0., 0, h, w]], device=input.device)
                    ],
                    dim=0)
                labels = torch.cat(
                    [
                        y.get_field('labels'),
                        torch.tensor([0], device=input.device)
                    ],
                    dim=0)
                bl = BoxList(boxes, labels=labels)
                new_targets.append(bl)
            targets = new_targets

            _targets = [bl.xyxy() for bl in targets]
            _targets = [{
                'boxes': bl.boxes,
                'labels': bl.get_field('labels')
            } for bl in _targets]
            loss_dict = self.model(input, _targets)
            loss_dict['total_loss'] = sum(list(loss_dict.values()))
            return loss_dict

        out = self.model(input)
        boxlists = [
            BoxList(
                _out['boxes'], labels=_out['labels'],
                scores=_out['scores']).yxyx() for _out in out
        ]

        # Remove bogus background boxes.
        new_boxlists = []
        for bl in boxlists:
            labels = bl.get_field('labels')
            non_zero_inds = labels != 0
            new_boxlists.append(bl.ind_filter(non_zero_inds))
        return new_boxlists
