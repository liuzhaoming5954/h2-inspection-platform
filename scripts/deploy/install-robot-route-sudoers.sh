#!/usr/bin/env bash
# 为现场专机写入一条最小化的 sudoers 规则：允许当前用户免密执行
# 「把指定网口上的主机路由指向机器人 PC」这一类命令，供 ~/h2-start.sh 使用。
#
# 横在局域网与机器人之间的专机有两个同网段网口，发往机器人的流量需要主机路由钉到
# 面向机器人的网口；添加路由需要 root，而一键启动不应中途索要密码。
# 规则只放行 ip route replace <机器人地址>/32 dev <网口> src <本机地址>，
# 不授予其他任何 sudo 权限。在专机上用 sudo 运行：
#   sudo bash scripts/deploy/install-robot-route-sudoers.sh <面向机器人的网口> <该网口的 IPv4 地址> [机器人地址 ...]
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <robot-facing interface> <its IPv4 address> [robot host ...]" >&2
  exit 2
fi
LINK="$1"; SRC="$2"; shift 2
HOSTS=("$@")
[ ${#HOSTS[@]} -gt 0 ] || HOSTS=(192.168.124.162 192.168.124.163)

ip link show "$LINK" >/dev/null
case "$SRC" in *.*) ;; *) echo "ERROR: invalid IPv4 address: $SRC" >&2; exit 2 ;; esac
# 规则以调用者本人为授权对象；用 sudo 运行时取真实用户
USER_NAME="${SUDO_USER:-$(id -un)}"
RULE_FILE="/etc/sudoers.d/h2-robot-route"
IP_BIN="$(command -v ip)"

TMP=$(mktemp)
trap 'rm -f -- "$TMP"' EXIT
{
  printf '%s\n' "# 由 h2-inspection-platform 生成：允许一键启动脚本把机器人主机路由固定到 $LINK。"
  printf 'Cmnd_Alias H2_ROBOT_ROUTE = '
  first=1
  for host in "${HOSTS[@]}"; do
    [ $first -eq 1 ] || printf ', '
    first=0
    printf '%s route replace %s/32 dev %s src %s' "$IP_BIN" "$host" "$LINK" "$SRC"
  done
  printf '\n'
  printf '%s ALL=(root) NOPASSWD: H2_ROBOT_ROUTE\n' "$USER_NAME"
} > "$TMP"
chmod 440 "$TMP"
visudo -cf "$TMP"   # 语法检查，避免写出无法解析的 sudoers
install -m 440 -o root -g root "$TMP" "$RULE_FILE"
echo "installed $RULE_FILE for $USER_NAME"
