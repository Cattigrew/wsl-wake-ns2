# wsl-wake-ns2

从 Windows 按一下鼠标宏，让 WSL 2 里的蓝牙适配器发送唤醒广播，尝试唤醒附近处于休眠状态的 Nintendo Switch 2。适合主机放在底座上、希望坐在电脑前就能唤醒它的场景。

## 准备

- Windows 上已安装 WSL 2 和 Ubuntu。以下命令假定发行版名为 `Ubuntu`，可用 `wsl.exe -l -v` 核对。
- WSL 内需要 Python 3.8 或以上版本、BlueZ 的 `btmgmt` 和一个可由 Linux 控制的 BLE 适配器。USB 蓝牙适配器可参考[微软的 WSL USB 连接说明](https://learn.microsoft.com/windows/wsl/connect-usb)。脚本只使用 Python 标准库。
- 准备好适用于自己主机和手柄组合的**广播源公共 MAC** 与 **31 字节唤醒载荷**。脚本不负责配对或生成载荷。仓库中的默认值仅用于展示格式。

在 PowerShell 中安装依赖并查看适配器：

```powershell
wsl.exe -d Ubuntu -u root -- apt-get update
wsl.exe -d Ubuntu -u root -- apt-get install -y bluez python3
wsl.exe -d Ubuntu -u root -- btmgmt info
```

记下对应的控制器编号，例如 `hci0`。如果要使用的源 MAC 与适配器原地址不同，控制器和驱动还需要支持设置公共地址，可在 `btmgmt info` 的 `supported options` 中查看 `public-address`。脚本运行期间会独占所选适配器，已有蓝牙连接可能中断。

## 用 Android 捕捉 Joy-Con 2 唤醒广播

参考项目使用 Android 手机上的 **nRF Connect for Mobile** 观察右 Joy-Con 2 按下 HOME 时短暂出现的 BLE 广播。手机只需扫描，手柄保持与自己的 Switch 2 配对即可。[捕捉方法来源：Switch2-RPI-Wake](https://github.com/Bill-git1/Switch2-RPI-Wake#capture-your-own-joy-con-2-wake-packet)

1. 将手机放在已配对的右 Joy-Con 2 旁边，打开 nRF Connect 并开始扫描。先看一眼此时有哪些设备，方便辨认稍后出现的广播。
2. 让 Switch 2 进入普通休眠。保持手机扫描，按一下右 Joy-Con 2 的 **HOME** 键，观察主机是否醒来。
3. 在扫描结果中展开按键时出现或更新的记录，查看 **Manufacturer Specific Data**。参考项目观察到的 Nintendo 公司标识为 `0x0553`，在原始广播中按小端顺序显示为 `53 05`。附近可能有其他 Nintendo 设备，结合按键时刻和手机靠近手柄时的信号强度判断。
4. 查看同一条记录的设备地址和完整原始广播数据。参考项目捕捉到的 31 字节广播以 `02 01 06 1B FF 53 05` 开头，其中 `02 01 06` 是 Flags 字段。仅复制厂商数据会漏掉开头的字段。若只看到较短的片段，回到完整广播视图核对；保留实际捕捉到的尾部零字节。

漏掉广播时，可让主机再次进入休眠，确认手机已经开始扫描后再按 HOME。没有 Android 手机时，上游也提供了使用树莓派和 BlueZ `btmon` 观察广播的方法。[树莓派捕捉说明](https://github.com/Bill-git1/Switch2-RPI-Wake#optional-capture-with-the-pi)

## 首次运行

在 PowerShell 中下载项目并进入目录：

```powershell
git clone https://github.com/Cattigrew/wsl-wake-ns2.git
cd wsl-wake-ns2
wsl.exe -d Ubuntu -- python3 bluez-wake.py --help
```

运行以下命令，在提示时输入自己的参数。MAC 是广播发送方的地址。载荷填写 62 个十六进制数字，可以包含空格，省略 `0x` 前缀。

```powershell
$wakeMac = Read-Host '广播源公共 MAC'
$wakeAdv = Read-Host '31 字节的唤醒载荷'
wsl.exe -d Ubuntu -u root -- python3 bluez-wake.py --hci hci0 --mac $wakeMac --adv $wakeAdv --seconds 1
```

| 参数 | 含义 |
| --- | --- |
| `--hci` | WSL 中的适配器编号，默认 `hci0` |
| `--mac` | 广播源公共 MAC，格式如 `02:00:00:00:00:01` |
| `--adv` | 完整的 31 字节广播数据，包含 AD 字段的长度和类型 |
| `--seconds` | 广播时长，默认 1 秒，范围 1–65535 秒 |

看到 `Advertising as ...` 表示控制器已接受地址核对、载荷设置和启用广播的命令。脚本会在指定时间后关闭广播，并尝试恢复、核对原公共地址、电源状态和名称。是否唤醒成功，以 Switch 2 的实际反应为准。

## 绑定鼠标宏

确认手动运行可以唤醒主机后，在项目目录中执行一次安装命令。指定的目标路径位于 WSL 内，因此鼠标宏可以从任意 Windows 工作目录调用：

```powershell
wsl.exe -d Ubuntu -u root -- install -D -o root -g root -m 0755 bluez-wake.py /usr/local/sbin/switch-wake
```

在鼠标软件中选择“启动程序”，填写下表。将两个尖括号占位符替换为首次测试通过的实际值，载荷使用不含空格的连续十六进制数字。

| 设置 | 值 |
| --- | --- |
| 程序 | `C:\Windows\System32\wsl.exe` |
| 参数 | `-d Ubuntu -u root --exec /usr/local/sbin/switch-wake --hci hci0 --mac <源MAC> --adv <62位十六进制载荷>` |
| 工作目录 | 留空 |

更新脚本后重新执行安装命令即可。若提示找不到 `hci0`，先运行 `btmgmt info` 核对设备是否已接入 WSL。若控制器接受了广播但主机没有反应，请核对源 MAC 和载荷是否属于同一组已验证的配置，并确认主机处于休眠状态且在蓝牙覆盖范围内。
