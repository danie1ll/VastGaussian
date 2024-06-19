#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#
import copy
import glob
import os
import logging
import numpy as np
import torch
from torchvision import transforms
from random import randint
from utils.loss_utils import l1_loss, ssim
from gaussian_renderer import render, network_gui
import sys
# from scene import Scene, GaussianModel
from VastGaussian_scene.datasets import PartitionScene, GaussianModel
from utils.general_utils import safe_state
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments.parameters import ModelParams, PipelineParams, OptimizationParams, extract, create_man_rans

from VastGaussian_scene.seamless_merging import seamless_merge
from multiprocessing import Process
import torch.multiprocessing as mp

from scene.dataset_readers import sceneLoadTypeCallbacks
from VastGaussian_scene.data_partition import ProgressiveDataPartitioning
from utils.camera_utils import cameraList_from_camInfos_partition


os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

try:
    from torch.utils.tensorboard import SummaryWriter

    TENSORBOARD_FOUND = False
except ImportError:
    TENSORBOARD_FOUND = False

WARNED = False


# https://github.com/autonomousvision/gaussian-opacity-fields
def decouple_appearance(image, gaussians, view_idx):
    appearance_embedding = gaussians.get_apperance_embedding(view_idx)
    H, W = image.size(1), image.size(2)
    # down sample the image
    crop_image_down = torch.nn.functional.interpolate(image[None], size=(H // 32, W // 32), mode="bilinear", align_corners=True)[0]

    crop_image_down = torch.cat([crop_image_down, appearance_embedding[None].repeat(H // 32, W // 32, 1).permute(2, 0, 1)], dim=0)[None]
    mapping_image = gaussians.appearance_network(crop_image_down, H, W).squeeze()
    transformed_image = mapping_image * image

    return transformed_image, mapping_image


def training(dataset, opt, pipe, testing_iterations, saving_iterations, checkpoint_iterations, checkpoint, debug_from, logger = None):
    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree)
    model_weights_dir = os.path.join("weights", f"{dataset.exp_name}")
    os.makedirs(model_weights_dir, exist_ok=True)  # 创建weights文件夹保存模型权重
    gaussians.load_DAM_model(model_weights_dir)  # 加载预训练的外观解耦模型

    scene = PartitionScene(dataset, gaussians)
    gaussians.training_setup(opt)
    if checkpoint:
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, opt)

    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)

    viewpoint_stack = None
    ema_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, opt.iterations), desc=f"Training progress Partition {dataset.partition_id}")
    first_iter += 1
    for iteration in range(first_iter, opt.iterations + 1):
        if network_gui.conn == None:
            network_gui.try_connect()
        while network_gui.conn != None:
            try:
                net_image_bytes = None
                custom_cam, do_training, pipe.convert_SHs_python, pipe.compute_cov3D_python, keep_alive, scaling_modifer = network_gui.receive()
                if custom_cam != None:
                    net_image = render(custom_cam, gaussians, pipe, background, scaling_modifer)["render"]
                    net_image_bytes = memoryview((torch.clamp(net_image, min=0, max=1.0) * 255).byte().permute(1, 2, 0).contiguous().cpu().numpy())
                network_gui.send(net_image_bytes, dataset.source_path)
                if do_training and ((iteration < int(opt.iterations)) or not keep_alive):
                    break
            except Exception as e:
                network_gui.conn = None

        iter_start.record()

        gaussians.update_learning_rate(iteration)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()

        # Pick a random Camera
        if not viewpoint_stack:
            viewpoint_stack = scene.getTrainCameras().copy()
        viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))

        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True

        bg = torch.rand((3), device="cuda")

        render_pkg = render(viewpoint_cam, gaussians, pipe, bg)
        image, viewspace_point_tensor, visibility_filter, radii = render_pkg["render"], render_pkg["viewspace_points"], render_pkg["visibility_filter"], render_pkg["radii"]
        # Loss
        gt_image = viewpoint_cam.original_image.cuda()

        # 外观解耦模型
        decouple_image, transformation_map = decouple_appearance(image, gaussians, viewpoint_cam.uid)

        Ll1 = l1_loss(decouple_image, gt_image)
        loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim(image, gt_image))
        loss.backward()

        iter_end.record()

        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log
            if iteration % 10 == 0:
                progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}"})
                progress_bar.update(10)
            if iteration == opt.iterations:
                progress_bar.close()

            # Log and save
            training_report(tb_writer, iteration, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), testing_iterations, scene, render, (pipe, background), logger=logger)
            if (iteration in saving_iterations):
                if logger is not None:
                    logger.info(f"Saving Gaussians at iteration {iteration}")
                print("\n[ITER {}] Saving Gaussians".format(iteration))
                scene.save(iteration)

            gaussians.save_DAM_model(iteration, dataset.pre_train_iteration, model_weights_dir, dataset.m_region*dataset.n_region)  # 在第pre_train_iteration次时保存DAM作为预训练权重
            if dataset.m_region*dataset.n_region and iteration == dataset.pre_train_iteration:
                print("[ITER {}] Saving pre-trained DAM model".format(iteration))
                break

            # Densification
            if iteration < opt.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    size_threshold = 20 if iteration > opt.opacity_reset_interval else None
                    gaussians.densify_and_prune(opt.densify_grad_threshold, 0.005, scene.cameras_extent, size_threshold)

                if iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                    gaussians.reset_opacity()

            # Optimizer step
            if iteration < opt.iterations:
                gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none = True)

            if (iteration in checkpoint_iterations):
                if logger is not None:
                    logger.info(f"Saving Checkpoint at iteration {iteration}")
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt" + str(iteration) + ".pth")

            # 每1000轮保存一次中间外观解耦图像
            if iteration % 1000 == 0:
                decouple_image = decouple_image.cpu()
                decouple_image = transforms.ToPILImage()(decouple_image)
                save_dir = os.path.join(scene.model_path, "decouple_images")
                if not os.path.exists(save_dir): os.makedirs(save_dir)
                decouple_image.save(f"{save_dir}/decouple_image_{dataset.partition_id}_{viewpoint_cam.uid}_{iteration}.png")

                transformation_map = transformation_map.cpu()
                transformation_map = transforms.ToPILImage()(transformation_map)
                transformation_map.save(
                    f"{save_dir}/transformation_map_{dataset.partition_id}_{viewpoint_cam.uid}_{iteration}.png")

                image = image.cpu()
                image = transforms.ToPILImage()(image)
                image.save(f"{save_dir}/render_image_{dataset.partition_id}_{viewpoint_cam.uid}_{iteration}.png")

def parallel_local_training(gpu_id, partition_id, lp_args, op_args, pp_args, test_iterations, save_iterations, checkpoint_iterations,
                            start_checkpoint, debug_from):
    torch.cuda.set_device(gpu_id)

    partition_model_path = f"{lp_args.model_path}/partition_point_cloud/visible"
    lp_args.partition_id = partition_id
    lp_args.partition_model_path = partition_model_path

    logger = setup_logging(partition_id, file_path=partition_model_path)
    # 启动训练
    logger.info("Starting process")
    training(lp_args, op_args, pp_args, test_iterations, save_iterations, checkpoint_iterations,start_checkpoint, debug_from, logger=logger)
    logger.info("Finishing process")

def setup_logging(process_id, file_path):
    # 创建一个 logger
    logger = logging.getLogger(f'Client_{process_id}')
    logger.setLevel(logging.INFO)  # 设置日志级别

    # 创建文件 handler，用于写入日志文件
    if not os.path.exists(file_path):
        os.makedirs(file_path)
    file_handler = logging.FileHandler(f'{file_path}/client_{process_id}.log')

    # 创建 formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)

    # 添加 handler 到 logger
    logger.addHandler(file_handler)

    return logger

def prepare_output_and_logger(args):
    # if not args.model_path:
    #     if os.getenv('OAR_JOB_ID'):
    #         unique_str = os.getenv('OAR_JOB_ID')
    #     else:
    #         unique_str = str(uuid.uuid4())
    #     args.model_path = os.path.join("./output/", unique_str[0:10])
    if not args.model_path:
        model_path = os.path.join("./output/", args.exp_name)
        # 如果这个文件存在，就在这个文件名的基础上创建新的文件夹，文件名后面跟上1,2,3
        if os.path.exists(model_path):
            base_name = os.path.basename(model_path)
            dir_name = os.path.dirname(model_path)
            file_name, file_ext = os.path.splitext(base_name)
            counter = 1
            while os.path.exists(os.path.join(dir_name, f"{file_name}_{counter}{file_ext}")):
                counter += 1
            new_folder_name = f"{file_name}_{counter}{file_ext}"
            model_path = os.path.join(dir_name, new_folder_name)
        args.model_path = model_path

    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok=True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        var_dict = copy.deepcopy(vars(args))
        del_var_list = ["manhattan", "man_trans", "pos", "rot",
                        "m_region", "n_region", "extend_rate", "visible_rate",
                        "num_gpus", "partition_id", "partition_model_path", "plantform",
                        "pre_train_iteration"]  # 删除多余的变量，防止无法使用SIBR可视化
        for key in vars(args).keys():
            if key in del_var_list:
                del var_dict[key]
        cfg_log_f.write(str(Namespace(**var_dict)))


    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer


def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : PartitionScene, renderFunc, renderArgs, logger = None):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar('iter_time', elapsed, iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = ({'name': 'test', 'cameras' : scene.getTestCameras()},
                              {'name': 'train', 'cameras' : [scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)]})

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    image = torch.clamp(renderFunc(viewpoint, scene.gaussians, *renderArgs)["render"], 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])
                if logger is not None:
                    logger.info("[ITER {}] Evaluating {}: L1 {} PSNR {}".format(iteration, config['name'], l1_test, psnr_test))
                print("\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(iteration, config['name'], l1_test, psnr_test))
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
        torch.cuda.empty_cache()


def train_main():
    """重写训练主函数
    将原先隐式的参数进行显式化重写，方便阅读和调参
    代码主体与原先仍保持一致
    参数详情见：arguments/parameters.py
    """
    parser = ArgumentParser(description="Training Script Parameters")
    # 三个模块里的参数
    lp = ModelParams(parser).parse_args()
    op, before_extract_op = extract(lp, OptimizationParams(parser).parse_args())
    pp, before_extract_pp = extract(before_extract_op, PipelineParams(parser).parse_args())

    if lp.manhattan and lp.plantform == "threejs":
        man_trans = create_man_rans(lp.pos, lp.rot)
        lp.man_trans = man_trans
    elif lp.manhattan and lp.plantform == "cloudcompare":  # 如果处理平台为cloudcompare，则rot为旋转矩阵
        rot = np.array(lp.rot).reshape([3, 3])
        man_trans = np.zeros((4, 4))
        man_trans[:3, :3] = rot
        man_trans[:3, -1] = np.array(lp.pos)
        man_trans[3, 3] = 1
        lp.man_trans = man_trans

    # train.py脚本显式参数
    parser.add_argument("--ip", type=str, default='127.0.0.1')  # 启动GUI服务器的IP地址，默认为127.0.0.1。
    parser.add_argument("--port", type=int, default=6009)  # 用于GUI服务器的端口，默认为6009。
    parser.add_argument("--debug_from", type=int, default=-1)  # 调试缓慢。您可以指定一个迭代(从0开始)，之后上述调试变为活动状态。
    parser.add_argument("--detect_anomaly", default=False)  #
    parser.add_argument("--test_iterations", nargs="+", type=int,
                        default=[100, 1000, 7_000, 10_000, 30_000])  # 训练脚本在测试集上计算L1和PSNR的间隔迭代，默认为7000 30000。
    parser.add_argument("--save_iterations", nargs="+", type=int,
                        default=[100, 7_000, 30_000, 60_000])  # 训练脚本保存高斯模型的空格分隔迭代，默认为7000 30000 <迭代>。
    parser.add_argument("--quiet", default=False)  # 标记以省略写入标准输出管道的任何文本。
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])  # 空格分隔的迭代，在其中存储稍后继续的检查点，保存在模型目录中。
    parser.add_argument("--start_checkpoint", type=str, default=None)  # 路径保存检查点继续训练。
    args = parser.parse_args()
    args.save_iterations.append(args.iterations)
    args.source_path = os.path.abspath(args.source_path)  # 将相对路径转换为绝对路径

    if args.manhattan:
        print("Need to perform Manhattan World Hypothesis based alignment")

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training
    network_gui.init(args.ip, args.port)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)

    mp.set_start_method('spawn', force=True)

    tb_writer = prepare_output_and_logger(lp)

    # 对大场景进行分块
    scene_info = sceneLoadTypeCallbacks["Partition"](lp.source_path, lp.images, lp.man_trans)  # 得到一个场景的所有参数信息
    train_cameras = cameraList_from_camInfos_partition(scene_info.train_cameras, args=lp)
    DataPartitioning = ProgressiveDataPartitioning(scene_info, train_cameras, lp.model_path,
                                                   lp.m_region, lp.n_region, lp.extend_rate, lp.visible_rate)
    partition_result = DataPartitioning.partition_scene
    # 保存每个partition的图片名称到txt文件
    client = 0
    partition_id_list = []
    for partition in partition_result:
        partition_id_list.append(partition.partition_id)
        camera_info = partition.cameras
        image_name_list = [camera_info[i].camera.image_name + '.jpg' for i in range(len(camera_info))]
        txt_file = f"{lp.model_path}/partition_point_cloud/visible/{partition.partition_id}_camera.txt"
        # 打开一个文件用于写入，如果文件不存在则会被创建
        with open(txt_file, 'w') as file:
            # 遍历列表中的每个元素
            for item in image_name_list:
                # 将每个元素写入文件，每个元素占一行
                file.write(f"{item}\n")
        client += 1
    del partition_result  # 释放内存

    training_round = client // lp.num_gpus
    remainder = client % lp.num_gpus  # 判断分块数是否可以被GPU均分，如果不可以均分则需要单独处理

    # Main Loops
    for i in range(training_round):
        partition_pool = [i + training_round * j for j in range(lp.num_gpus)]

        processes = []
        for index, device_id in enumerate(range(lp.num_gpus)):
            partition_index = partition_pool[index]
            partition_id = partition_id_list[partition_index]
            print("train partition {} on gpu {}".format(partition_id, device_id))
            p = Process(target=parallel_local_training, name=f"Partition_{partition_id}",
                    args=(device_id, partition_id, lp, op, pp,
                          args.test_iterations, args.save_iterations, args.checkpoint_iterations,
                          args.start_checkpoint, args.debug_from))
            processes.append(p)
            p.start()

        for p in processes:
            p.join()  # 等待所有进程完成
            # processes = []

        torch.cuda.empty_cache()

    if remainder != 0:
        partition_pool = [lp.num_gpus*training_round + i for i in range(remainder)]
        processes = []
        for index, device_id in enumerate(range(lp.num_gpus)[:remainder]):
            # torch.cuda.set_device(device_id)
            partition_index = partition_pool[index]
            partition_id = partition_id_list[partition_index]
            print("train partition {} on gpu {}".format(partition_id, device_id))
            p = Process(target=parallel_local_training, name=f"Partition_{partition_id}",
                        args=(device_id, partition_id, lp, op, pp,
                              args.test_iterations, args.save_iterations, args.checkpoint_iterations,
                              args.start_checkpoint, args.debug_from))
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        torch.cuda.empty_cache()

    print("\nTraining complete.")

    # seamless_merging 无缝合并
    print("Merging Partitions...")
    all_point_cloud_dir = glob.glob(os.path.join(lp.model_path, "point_cloud", "*"))

    for point_cloud_dir in all_point_cloud_dir:
        seamless_merge(lp.model_path, point_cloud_dir)

    print("All Done!")



if __name__ == "__main__":
    train_main()


