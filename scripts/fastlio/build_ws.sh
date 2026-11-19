#!/bin/bash
# 在容器内编译建图工作区（PC3 首次启动执行一次，之后复用 devel 结果）。
# 依赖已全部烘焙在镜像中，本脚本不需要网络。
set -e
source /opt/ros/noetic/setup.bash
WS=/workspace/G1Nav2D

# livox_ros_driver2 的包描述按 ROS1 启用（仓库两种 ROS 共用一份源码）
[ -f "$WS/src/livox_ros_driver2-master/package.xml" ] \
  || cp "$WS/src/livox_ros_driver2-master/package_ROS1.xml" "$WS/src/livox_ros_driver2-master/package.xml"

# 删除残留的 src/CMakeLists.txt（与官方 build.sh 同一惯例）：
# catkin_make 需要自己生成 toplevel 符号链接，残留空文件会让整个工作区
# 被当成无包的普通 cmake 工程，所有目标都不生成。
[ ! -L "$WS/src/CMakeLists.txt" ] && rm -f "$WS/src/CMakeLists.txt"

# 只保留建图两包参与构建：其余导航/工具包放 CATKIN_IGNORE 标记
# （依赖 rviz 等本镜像未装的组件，且建图不需要；删掉标记即可恢复全量构建）。
KEEP="fastlio2 livox_ros_driver2-master"
for dir in "$WS"/src/*/; do
    name="$(basename "$dir")"
    if [ "$name" = "CMakeLists.txt" ]; then continue; fi
    case " $KEEP " in
        *" $name "*) rm -f "$dir/CATKIN_IGNORE" ;;
        *) touch "$dir/CATKIN_IGNORE" ;;
    esac
done

# 只编建图所需目标：map_builder_node（FastLIO 建图）+ livox_ros_driver2_node（雷达驱动）。
# fastlio 的 localizer_node/temp_node 属定位与调试用途，其中 localizer_node 在
# noetic 下有编译错误（advertiseService 模板推导异常），且建图不依赖，直接跳过。
# ROS_EDITION=ROS1 是 livox CMakeLists 的分支开关（与官方 build.sh 一致），
# 不传会走 ROS2/ament 分支而失败。
cd "$WS"
# 分三段：先生成两包的消息/服务头（CustomMsg.h、SaveMap.h 等），再编驱动，
# 最后编建图节点。上游 CMakeLists 未把消息生成挂进可执行目标的依赖链，
# 直接 make 目标节点会因头文件缺失失败。
catkin_make -DROS_EDITION=ROS1 -DCMAKE_BUILD_TYPE=Release \
  --make-args fastlio_generate_messages_cpp livox_ros_driver2_generate_messages_cpp
catkin_make -DROS_EDITION=ROS1 -DCMAKE_BUILD_TYPE=Release --make-args livox_ros_driver2_node
catkin_make -DROS_EDITION=ROS1 -DCMAKE_BUILD_TYPE=Release \
  --make-args map_builder_node livox_ros_driver2_node

echo "编译完成：source $WS/devel/setup.bash"
