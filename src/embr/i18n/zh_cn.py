"""Simplified Chinese interface text: English key -> translation (design doc §12.5).

Keys are the English source text; formatted strings use template keys with {0}, {1}…
(i18n.template_key). Words the glossary keeps in English (internal_docs/m7_glossary.md)
stay English inside the translations."""

TEXT: dict[str, str] = {
    # ---- checks (app.checklist, core.flasher.checks) ----
    "Chip / revision / size / security": "芯片 / 版本 / 容量 / 安全",
    "checked after connecting": "连接后检查",
    "the firmware is for {0}, the board is {1} {2}": "固件适用于 {0}，设备是 {1} {2}",
    "No address overlap": "地址无重叠",
    "Entry layout": "条目布局",
    "enable at least one entry for {0}": "请为 {0} 至少勾选一个条目",
    "enable at least one entry": "请至少勾选一个条目",
    "No entries enabled": "没有勾选的条目",
    "File missing": "文件缺失",
    "{0}: not found at the recorded path. Re-locate or remove the entry.": (
        "{0}：在记录的路径下找不到。请重新定位或删除该条目。"
    ),
    "Needs confirmation": "需确认",
    "{0}: content on disk differs from the recorded hash. Review the entries, then confirm.": (
        "{0}：磁盘上的内容与记录的 hash 不一致。请核对条目后确认。"
    ),
    "Files match": "文件一致",
    "{0} files checked: name, path and hash agree with the recorded values": (
        "已核对 {0} 个文件：名称、路径与 hash 均与记录一致"
    ),
    "Build resolved": "构建已解析",
    "Reset policy adjusted to connection": "复位策略已按连接类型调整",
    "Reset policy": "复位策略",
    "{0} has no enabled entries": "{0} 没有勾选的条目",
    "{0} has no address": "{0} 没有地址",
    "{0}: flash size unknown; set it to pad the image": (
        "{0}：flash 容量未知；要填充镜像请先设置容量"
    ),
    "{0} not found": "找不到 {0}",
    "{0} is older than {1}": "{0} 比 {1} 旧",
    "{0} cannot be read ({1})": "无法读取 {0}（{1}）",
    "{0} lacks extra.flash_images or extra.application_offset": (
        "{0} 缺少 extra.flash_images 或 extra.application_offset"
    ),
    "{0} has a malformed flash_images entry": "{0} 中有格式错误的 flash_images 条目",
    "{0} lists missing files: {1}": "{0} 列出的文件不存在：{1}",
    "pio was not found (install PlatformIO or add it to PATH)": (
        "找不到 pio（请安装 PlatformIO 或将其加入 PATH）"
    ),
    "The build output is still being written; flash again in a moment.": (
        "构建产物仍在写入；请稍后再烧录。"
    ),
    "{0} changed since it was shown ({1}); review it and press Flash again.": (
        "{0} 在显示（{1}）之后已变化；请核对后再按 Flash。"
    ),
    "This firmware type cannot be resolved here": "此类型的固件无法在此解析",
    "No build directory": "没有构建目录",
    "{0}: build output not found": "{0}：找不到构建产物",
    "No PlatformIO project and env": "没有 PlatformIO 工程与 env",
    "more than one partition table is enabled": "勾选了多个分区表",
    "{0} is not a partition table": "{0} 不是分区表",
    "{0}: partition '{1}' not found": "{0}：找不到分区“{1}”",
    "{0} ({1} bytes) does not fit partition '{2}' ({3})": ("{0}（{1} 字节）放不进分区“{2}”（{3}）"),
    "{0} and {1} overlap": "{0} 与 {1} 地址重叠",
    "{0} ends at {1}, beyond the flash size": "{0} 结束于 {1}，超出 flash 容量",
    "{0}: flash encryption is enabled in this project; Embr does not support it": (
        "{0}：此工程启用了 flash 加密，Embr 不支持"
    ),
    "{0}: expected a JSON object": "{0}：应为 JSON 对象",
    "flash_files: bad entry for {0}": "flash_files：{0} 的条目无效",
    "flash_files: {0} is not an address": "flash_files：{0} 不是地址",
    "{0} lists no flash files": "{0} 没有列出要烧录的文件",
    "{0} is not a .pio/build/<env> folder of a PlatformIO project": (
        "{0} 不是 PlatformIO 工程的 .pio/build/<env> 文件夹"
    ),
    "{0} has no {1}": "{0} 中没有 {1}",
    "{0} in {1} does not declare [env:{2}]": "{1} 中的 {0} 没有声明 [env:{2}]",
    "pio project config printed no JSON ({0})": "pio project config 没有输出 JSON（{0}）",
    "pio project config printed unexpected JSON": "pio project config 输出的 JSON 格式不符",
    "pio did not answer within {0} s": "pio 在 {0} 秒内没有响应",
    "pio could not be started: {0}": "无法启动 pio：{0}",
    "pio exited with code {0}: {1}": "pio 退出，代码 {0}：{1}",
    "framework package not found: {0} has no {1}": "找不到框架包：{0} 中没有 {1}",
    "{0}: no build output": "{0}：没有构建产物",
    "{0} no longer declares [env:{1}]": "{0} 已不再声明 [env:{1}]",
    "{0}; the build folder was scanned instead": "{0}；已改为扫描构建文件夹",
    "{0}: a merged image; left out": "{0}：合成镜像，未加入",
    "{0}: not recognised; left out": "{0}：无法识别，未加入",
    "{0} left out: the partition table has no otadata partition": (
        "未加入 {0}：分区表中没有 otadata 分区"
    ),
    "{0} was not found in any framework package; a blank otadata is used": (
        "所有框架包中都找不到 {0}；改用空白 otadata"
    ),
    "flash settings differ: bootloader {0}, app {1} (the bootloader's are used)": (
        "flash 参数不一致：bootloader 为 {0}，app 为 {1}（采用 bootloader 的）"
    ),
    "{0}: bad flash_files entry {1}": "{0}：flash_files 条目 {1} 无效",
    "{0}: file missing": "{0}：文件缺失",
    "{0}: cannot be read as {1}": "{0}：无法按 {1} 读取",
    "the build output has no app image": "构建产物中没有 app 镜像",
    "Target chip: {0} -> {1}": "目标芯片：{0} -> {1}",
    "The device group may need adjusting": "设备分组可能需要调整",
    "ESP-IDF: {0} -> {1}": "ESP-IDF：{0} -> {1}",
    "Flash: {0} -> {1}": "flash 参数：{0} -> {1}",
    "Reset: {0} -> {1}": "复位：{0} -> {1}",
    "Partition table content changed": "分区表内容已变化",
    "New file: {0}": "新文件：{0}",
    "{0}: chip revision {1} -> {2}": "{0}：芯片版本 {1} -> {2}",
    "{0}: address {1} -> {2} (manual address {3} kept)": (
        "{0}：地址 {1} -> {2}（保留手动地址 {3}）"
    ),
    "{0}: address {1} -> {2}": "{0}：地址 {1} -> {2}",
    "{0}: content changed": "{0}：内容已变化",
    "Removed: {0}": "已移除：{0}",
    "{0}: your changes no longer match a file and are dropped": (
        "{0}：所做的修改已无对应的文件，将被丢弃"
    ),
    "{0}: manual address {1} dropped on confirmation": "{0}：手动地址 {1} 将在确认时丢弃",
    "{0}: manual address {1} kept; check it": "{0}：手动地址 {1} 已保留，请核对",
    "{0} is enabled; Embr does not flash such chips, use esptool / espsecure": (
        "{0} 已启用；Embr 不烧录这类芯片，请使用 esptool / espsecure"
    ),
    "secure download mode, flash encryption and secure boot are off": (
        "安全下载模式、flash 加密与安全启动均未启用"
    ),
    "chip mismatch ({0})": "芯片不符（{0}）",
    "{0}: board, firmware and images agree": "{0}：设备、固件与镜像一致",
    "chip {0}, all entries in range": "芯片 {0}，全部条目在范围内",
    "flash size could not be detected": "无法检测 flash 容量",
    "firmware declares {0}, board has {1}": "固件声明 {0}，设备为 {1}",
    "board has {0}": "设备为 {0}",
    "partition table ends at {0}, beyond {1}": "分区表结束于 {0}，超出 {1}",
    "{0} ends at {1}, beyond {2}": "{0} 结束于 {1}，超出 {2}",
    "all entries fit in {0}": "全部条目都在 {0} 以内",
    "{0}: file missing ({1})": "{0}：文件缺失（{1}）",
    "{0}: content changed since it was checked": "{0}：核对后内容已变化",
    "{0} files: name, path and hash match": "{0} 个文件：名称、路径与 hash 一致",
    "nothing to merge": "没有可合成的条目",
    "{0} at {1} is below the start offset {2}": "{0} 位于 {1}，低于起始偏移 {2}",
    # ---- right panel (ui.connection) ----
    "UART bridge": "UART 桥",
    "serial port": "串口",
    "Click to dismiss": "点击关闭",
    "Connect && identify": "连接并识别",
    "Available after Connect & identify on this port": "在此端口连接并识别后可用",
    "Erase flash…": "擦除 flash…",
    "Read flash…": "读取 flash…",
    "Board log": "设备日志",
    "Flash, erase and read operations on this board (logs/boards/<MAC>.log)": (
        "此设备的烧录、擦除与读取记录（logs/boards/<MAC>.log）"
    ),
    "Port": "端口",
    "Baud": "波特率",
    "Checks": "检查",
    "Erase the whole flash first": "先擦除整片 flash",
    "Saved with the firmware. Clears NVS (Wi-Fi credentials and other stored data)": (
        "随固件保存。会清空 NVS（Wi-Fi 凭据和其他已保存的数据）"
    ),
    "Idle": "空闲",
    "No serial port found": "未找到串口",
    "not applicable": "不适用",
    "Native USB: baud rate not applicable": "原生 USB：不适用波特率",
    "Not identified yet. After identifying, firmware for other chips is dimmed in the sidebar.": (
        "尚未识别。识别后，侧栏中适用于其他芯片的固件会变灰。"
    ),
    "secure download mode": "安全下载模式",
    "flash encryption": "flash 加密",
    "secure boot": "安全启动",
    "in download mode": "in download mode",
    "reset, running": "已复位，正在运行",
    "Chip": "芯片",
    "Chip revision": "芯片版本",
    "Header flash size": "镜像头 flash 容量",
    "Flash capacity": "flash 容量",
    "Flash": "Flash",
    "{0} (detected)": "{0}（检测值）",
    "Security": "安全",
    "none (SDM / encryption / secure boot off)": "无（SDM / 加密 / 安全启动均未启用）",
    "Link": "连接",
    "State": "状态",
    "Identified": "已识别",
    "{0} · port released": "{0} · 端口已释放",
    "Select a firmware in the sidebar to run checks and flash.": (
        "在侧栏中选择固件后即可检查并烧录。"
    ),
    # ---- dialogs ----
    "Cancel": "取消",
    "Import into Embr as a firmware": "导入 Embr 作为固件",
    "Close": "关闭",
    "Standalone (no project)": "独立（不放进项目）",
    "Place in": "放入",
    "Group": "分组",
    "(ungrouped)": "（未分组）",
    "New firmware": "新建固件",
    "Create": "创建",
    "e.g. S3-main-v1.3 (default: the first file added)": "例如 S3-main-v1.3（默认：第一个加入的文件名）",
    "Name": "名称",
    "Fixed firmware: add entries by hand afterwards. Reset policy and erase_all are copied from the project defaults.": (
        "固定型固件：之后手动添加条目。复位策略与 erase_all 取自项目默认值。"
    ),
    "New project": "新建项目",
    "e.g. 500_roller_hmi": "例如 500_roller_hmi",
    "hardware revision, purpose…": "硬件版本、用途…",
    "Name (unique)": "名称（不可重复）",
    "Notes": "备注",
    "Defaults for new firmware: before=default-reset · after=hard-reset · erase_all=off": (
        "新固件的默认值：before=default-reset · after=hard-reset · erase_all=off"
    ),
    "Move firmware": "移动固件",
    "Move": "移动",
    "Only the project and the group change; entries, files and records stay as they are. Or drag the firmware in the sidebar.": (
        "只改变所在的项目与分组，条目、文件与记录保持不变。也可以在侧栏中拖动固件。"
    ),
    "Settings": "设置",
    "auto-detect (now: {0})": "自动检测（当前：{0}）",
    "auto-detect (nothing found)": "自动检测（未找到）",
    "Browse…": "浏览…",
    "Framework package": "框架包",
    "PlatformIO framework package (the framework-arduinoespressif32 folder that boot_app0.bin of Arduino firmware comes from). Empty: auto-detect. A firmware can override it on its Parameters tab.": (
        "PlatformIO 框架包（Arduino 固件的 boot_app0.bin 所在的 "
        "framework-arduinoespressif32 文件夹）。留空：自动检测。单个固件可在其参数页另行指定。"
    ),
    "Program folder": "程序文件夹",
    "Data and log folders (portable, inside the program folder; fixed, not configurable)": (
        "数据与日志文件夹（便携，位于程序文件夹内；位置固定，不可更改）"
    ),
    "About Embr": "关于 Embr",
    "Embr {0} · built-in esptool {1} · GPL-3.0-or-later": (
        "Embr {0} · 内置 esptool {1} · GPL-3.0-or-later"
    ),
    "Repository": "代码仓库",
    "Releases": "发布页面",
    "Interface font: HarmonyOS Sans (Copyright 2021 Huawei Device Co., Ltd.), used under the HarmonyOS Sans Fonts License Agreement.": (
        "界面字体：HarmonyOS Sans（Copyright 2021 Huawei Device Co., Ltd.），"
        "依据 HarmonyOS Sans Fonts License Agreement 使用。"
    ),
    "Third-party notices: THIRD_PARTY_NOTICES in the program folder.": (
        "第三方声明：见程序文件夹中的 THIRD_PARTY_NOTICES。"
    ),
    "Import ESP-IDF build": "导入 ESP-IDF 构建",
    "Import": "导入",
    "e.g. D:\\work\\my_project\\build": "例如 D:\\work\\my_project\\build",
    "Build directory (contains flasher_args.json)": "构建目录（含 flasher_args.json）",
    "Use the build folder": "改用 build 目录",
    "Show that firmware": "转到该固件",
    "The firmware follows the build: entries, chip, flash settings and reset modes are read from it each time. Rebuilds that only change file contents are followed automatically; other changes need confirming.": (
        "固件跟随构建：每次都从构建产物读取条目、芯片、flash 参数与复位策略。"
        "重新编译后只有文件内容变化时自动跟随；其他变化需要确认。"
    ),
    "<b>This is the project folder.</b> Pick its build folder, which contains flasher_args.json: {0}": (
        "<b>这是工程文件夹。</b>请选择含 flasher_args.json 的 build 目录：{0}"
    ),
    "<b>'{0}' already follows this build folder.</b> A build folder is followed by one firmware only.": (
        "<b>“{0}”已在跟随此构建目录。</b>一个构建目录只能被一个固件跟随。"
    ),
    "{0} · {1} · ESP-IDF {2} · {3} {4} {5}<br>{6}": "{0} · {1} · ESP-IDF {2} · {3} {4} {5}<br>{6}",
    "Build directory": "构建目录",
    "Import PlatformIO build": "导入 PlatformIO 构建",
    "e.g. D:\\work\\my_project\\.pio\\build\\esp32dev": "例如 D:\\work\\my_project\\.pio\\build\\esp32dev",
    "Env build folder (.pio\\build\\<env>)": "env 构建文件夹（.pio\\build\\<env>）",
    "Use this env": "改用此 env",
    "Envs (each checked env becomes a firmware named after it)": (
        "env（每个勾选的 env 生成一个以它命名的固件）"
    ),
    "Each firmware follows its env's build output: entries and flash settings are read from the files PlatformIO wrote. Embr never builds; when the board or the files change, it tells you. Rebuilds that only change file contents are followed automatically; other changes need confirming.": (
        "每个固件跟随其 env 的构建产物：条目与 flash 参数读取自 PlatformIO 生成的文件。"
        "Embr 不会编译；设备或文件变化时会给出提示。"
        "重新编译后只有文件内容变化时自动跟随；其他变化需要确认。"
    ),
    "<b>This is the project folder.</b> Pick an env build folder under .pio\\build: {0}": (
        "<b>这是工程文件夹。</b>请选择 .pio\\build 下的 env 构建文件夹：{0}"
    ),
    "Use {0}": "改用 {0}",
    "Reading the PlatformIO project…": "正在读取 PlatformIO 工程…",
    "Project {0}": "工程 {0}",
    "pio project config gave nothing ({0}); boards are unknown and the board check is skipped until it works.": (
        "pio project config 没有结果（{0}）；设备未知，在它恢复之前跳过设备检查。"
    ),
    "{0} · not built": "{0} · 未构建",
    "{0} · already followed by '{1}'": "{0} · 已被“{1}”跟随",
    "Env build folder": "env 构建文件夹",
    "board unknown": "设备未知",
    "chip unknown": "芯片未知",
    "Archive this version": "归档此版本",
    "Archive": "归档",
    "why this version is kept": "保留此版本的原因",
    "A new archived firmware is created next to <b>{0}</b>, which stays as it is. Files copied into data/archive/:{1}": (
        "将在 <b>{0}</b> 旁边新建一个归档型固件，原固件保持不变。复制到 data/archive/ 的文件：{1}"
    ),
    "Clean archive": "清理归档",
    "Delete files": "删除文件",
    "The archive holds {0} files ({1} bytes).": "归档区共有 {0} 个文件（{1} 字节）。",
    "{0} files are kept because firmware refers to them: {1}. Archived versions are firmware too; delete one of them first to free its files.": (
        "{0} 个文件被固件引用，因此保留：{1}。归档的版本也是固件；要释放其文件，请先删除对应的固件。"
    ),
    "<br>…and {0} more": "<br>…另有 {0} 项",
    "<b>Delete {0} files ({1} bytes) that no firmware refers to?</b> This cannot be undone.{2}{3}": (
        "<b>删除 {0} 个没有固件引用的文件（{1} 字节）？</b>此操作无法撤销。{2}{3}"
    ),
    "<b>Nothing to clean:</b> every archived file is in use.": (
        "<b>没有可清理的文件：</b>所有归档文件都在使用中。"
    ),
    "Merge to .bin": "合成为 .bin",
    "Merge": "合成",
    "Start offset (hex)": "起始偏移（十六进制）",
    "pad to the whole flash size": "填充到整片 flash 大小",
    "Save as": "保存为",
    "Gaps are filled with 0xFF, so every partition inside the written range is overwritten (the NVS between the partition table and otadata is cleared). A padded image overwrites the whole flash.": (
        "间隙用 0xFF 填充，写入范围内的分区都会被覆盖（分区表与 otadata 之间的 NVS 会被清空）。"
        "填充到整片的镜像会覆盖整片 flash。"
    ),
    "The start offset is not a hex number.": "起始偏移不是十六进制数。",
    "Save merged image": "保存合成镜像",
    "Binary files (*.bin)": "二进制文件 (*.bin)",
    "Erase flash": "擦除 flash",
    "Erase the whole flash of the {0} on <b>{1}</b> (MAC {2})?": (
        "擦除 <b>{1}</b> 上 {0} 的整片 flash（MAC {2}）？"
    ),
    "Everything is cleared, including NVS (Wi-Fi credentials and other stored data); the firmware on the device will no longer start. A record is kept.": (
        "所有内容都会被清除，包括 NVS（Wi-Fi 凭据和其他已保存的数据）；设备上的固件将无法启动。会保留一条记录。"
    ),
    "Read flash": "读取 flash",
    "Read": "读取",
    "whole flash ({0})": "整片 flash（{0}）",
    "Start address (hex)": "起始地址（十六进制）",
    "Size (hex)": "大小（十六进制）",
    "Save to": "保存到",
    "Imported, the content is also kept in the archive as a new fixed firmware with one merged entry. A dump includes NVS and may contain Wi-Fi credentials and other secrets.": (
        "导入后，内容还会存入归档区，生成一个含单个 merged 条目的固定型固件。"
        "读出的内容包含 NVS，可能含有 Wi-Fi 凭据等敏感数据。"
    ),
    "Save flash content": "保存 flash 内容",
    "Replace file": "替换文件",
    "<b>{0}</b> already exists in {1}. Replace it?": "<b>{0}</b> 已存在于 {1}。要替换吗？",
    "Replace": "替换",
    # ---- window frame, log, project view, sidebar ----
    "Menu": "菜单",
    "Hide sidebar (Ctrl+B)": "隐藏侧栏 (Ctrl+B)",
    "Minimize": "最小化",
    "Maximize": "最大化",
    "Restore": "还原",
    "Copy": "复制",
    "Equivalent command": "等效命令",
    "Copied": "已复制",
    "Show sidebar (Ctrl+B)": "显示侧栏 (Ctrl+B)",
    "Expand": "展开",
    "Log folder": "日志文件夹",
    "Log": "日志",
    "unique across all projects": "在所有项目中唯一",
    "Defaults for new firmware": "新固件的默认值",
    "Groups": "分组",
    "Delete project…": "删除项目…",
    "erase all before flashing": "烧录前擦除整片",
    "copied once when a firmware is created here; changing them later does not touch existing firmware": (
        "仅在此处新建固件时复制一次；之后修改不影响已有固件"
    ),
    "no groups yet": "还没有分组",
    "Remove": "移除",
    "Remove group (its firmware becomes ungrouped)": "移除分组（其中的固件改为未分组）",
    "{0} firmware": "{0} 个固件",
    "new group, e.g. Main board": "新分组，例如 主板",
    "Add": "添加",
    "Groups are display folders inside this project. Drag a firmware onto a group in the sidebar to move it there; drop it on the project name to make it ungrouped.": (
        "分组是项目内用于展示的文件夹。在侧栏中将固件拖到分组上即可移入；拖到项目名上则改为未分组。"
    ),
    "Firmware in this project ({0})": "此项目中的固件（{0}）",
    "Ungrouped": "未分组",
    "(empty)": "（空）",
    "(none)": "（无）",
    "{0} firmware in it will stay in the project as <b>Ungrouped</b>. Nothing is deleted.": (
        "其中的 {0} 个固件会留在项目中，改为<b>未分组</b>。不会删除任何内容。"
    ),
    "The group is empty.": "该分组为空。",
    "Remove group": "移除分组",
    "Remove the group <b>{0}</b> from {1}?": "从 {1} 中移除分组 <b>{0}</b>？",
    "Delete project": "删除项目",
    "Delete <b>{0}</b>? By default its {1} firmware become standalone.": (
        "删除 <b>{0}</b>？默认情况下，其中的 {1} 个固件会变为独立固件。"
    ),
    "Archived files are kept (use Clean archive); flash records are not affected.": (
        "归档文件会保留（可用“清理归档”删除）；烧录记录不受影响。"
    ),
    "Also delete all firmware in this project": "同时删除此项目中的所有固件",
    "Search projects and firmware": "搜索项目和固件",
    "{0} New   firmware · project": "{0} 新建   固件 · 项目",
    "{0} New": "{0} 新建",
    "fixed, add entries by hand": "固定型，手动添加条目",
    "Import ESP-IDF build…": "导入 ESP-IDF 构建…",
    "follows a build directory": "跟随一个构建目录",
    "Import PlatformIO build…": "导入 PlatformIO 构建…",
    "follows the build folders of PlatformIO envs": "跟随 PlatformIO env 的构建文件夹",
    "PROJECTS": "项目",
    "(no groups, no firmware)": "（没有分组，也没有固件）",
    "empty": "空",
    "STANDALONE FIRMWARE": "独立固件",
    "(none — drop firmware here to detach it from its project)": (
        "（无——将固件拖到此处即可脱离其项目）"
    ),
    "flashing": "正在烧录",
    "{0}\nNot for the identified board: {1}": "{0}\n不适用于已识别的设备：{1}",
    # ---- main window ----
    "Done": "完成",
    "Refused": "已拒绝",
    "Failed": "失败",
    "Cancelled": "已取消",
    "(identify the board, or add entries with addresses, to see the command)": (
        "（识别设备，或添加带地址的条目后，即可显示命令）"
    ),
    "Flashed and verified. If the device did not restart on its own (usually when download mode was entered by hand), reset it manually.": (
        "已烧录并校验。如果设备没有自行重启（通常是手动进入下载模式的情况），请手动复位。"
    ),
    "Identified. The chip stays in download mode so the next flash can start right away; reset the board to return to its application.": (
        "已识别。芯片停在下载模式，下一次烧录可以立即开始；复位设备即可返回应用程序。"
    ),
    "Connect & identify this board first; Flash becomes available when no check refuses.": (
        "请先连接并识别此设备；没有检查项拒绝时，Flash 即可用。"
    ),
    "Connect & identify this board first. USB-OTG: put the board into download mode as described by its vendor before connecting; Cancel is available while waiting.": (
        "请先连接并识别此设备。USB-OTG：连接前按设备厂商的说明让设备进入下载模式；等待时可以取消。"
    ),
    "Connect & identify this board first. USB-Serial/JTAG: automatic reset is tried; if the running firmware does not keep this peripheral, the connect step times out, then enter download mode as described by the board's vendor.": (
        "请先连接并识别此设备。USB-Serial/JTAG：会尝试自动复位；若运行中的固件未保留此外设，"
        "连接会超时，此时请按设备厂商的说明进入下载模式。"
    ),
    "Create a firmware or a project with {0} New in the sidebar.": "用侧栏的 {0} 新建 创建固件或项目。",
    "the project": "该项目",
    "standalone firmware": "独立固件",
    "Rename firmware": "重命名固件",
    "Unique within {0}; a duplicate gets a numeric suffix. Flash records keep the name used at the time.": (
        "在{0}内唯一；重名时自动加数字后缀。烧录记录保留当时的名称。"
    ),
    "Rename": "重命名",
    "Delete firmware": "删除固件",
    "Delete <b>{0}</b>? Archived files are kept (use Clean archive to remove unreferenced ones) and flash records are not affected.": (
        "删除 <b>{0}</b>？归档文件会保留（可用“清理归档”删除未被引用的文件），烧录记录不受影响。"
    ),
    "Delete": "删除",
    "(select a firmware to see its command)": "（选择固件后显示其命令）",
    "Connecting…": "正在连接…",
    "0x{0} already in flash, skipped": "0x{0} 已在 flash 中，跳过",
    "Reading from 0x{0}": "正在从 0x{0} 读取",
    "Writing 0x{0} {1} ({2}/{3})": "正在写入 0x{0} {1}（{2}/{3}）",
    "Nothing added": "没有添加任何内容",
    "This firmware is being flashed.": "此固件正在烧录。",
    "No files were dropped.": "没有拖入文件。",
    "Name shown in the sidebar": "在侧栏中显示的名称",
    "{0} file(s) from {1}": "来自 {1} 的 {0} 个文件",
    "{0}: drop a build folder on a project or group": "{0}：请将构建文件夹拖到项目或分组上",
    "Settings…": "设置…",
    "Clean archive…": "清理归档…",
    "Theme": "主题",
    "Language": "语言",
    "Restart": "重启",
    "OK": "确定",
    "The new language is used after Embr restarts.": "重启 Embr 后使用新的语言。",
    "A job is running: the choice is saved; Restart becomes available when it ends.": (
        "有任务正在运行：选择已保存；任务结束后即可重启。"
    ),
    "System": "跟随系统",
    "Light": "浅色",
    "Dark": "深色",
    "<b>{0}</b>: a folder without an ESP-IDF or PlatformIO build in it": (
        "<b>{0}</b>：文件夹中没有 ESP-IDF 或 PlatformIO 构建"
    ),
    "<b>{0}</b>: not found": "<b>{0}</b>：找不到",
    "<b>{0}</b>: a shortcut; drop the file it points to": "<b>{0}</b>：这是快捷方式；请拖入它指向的文件",
    "<b>{0}</b>: not a .bin file. To flash another kind of file, open a firmware and use Add entry, then Attachment, on its Entries tab.": (
        "<b>{0}</b>：不是 .bin 文件。要烧录其他类型的文件，请打开固件，在条目页用“添加条目”中的“附件”。"
    ),
    "<b>{0}</b>: cannot be read ({1})": "<b>{0}</b>：无法读取（{1}）",
    "<b>{0}</b>: empty file (0 bytes)": "<b>{0}</b>：空文件（0 字节）",
    "Drop a build folder on its own.": "请单独拖入构建文件夹。",
    # ---- workspace ----
    "content changed": "内容已变化",
    "new file": "新文件",
    "file missing": "文件缺失",
    "fixed": "固定型",
    "archived": "已归档",
    "tracks IDF build": "跟随 IDF 构建",
    "tracks PIO build": "跟随 PIO 构建",
    "target switched": "目标已切换",
    "build incomplete": "构建未完成",
    "Standalone": "独立",
    "Firmware actions": "固件操作",
    "Rename…\tF2": "重命名…\tF2",
    "Duplicate": "创建副本",
    "Move to project…": "移到项目…",
    "Archive this version…": "归档此版本…",
    "Delete…": "删除…",
    "needs confirmation": "需确认",
    "build output missing": "构建产物缺失",
    "Build updated {0}": "构建已更新 {0}",
    "created by a newer Embr": "由更新版本的 Embr 创建",
    "no note — edit on the Parameters tab": "无备注——在参数页编辑",
    "No note yet. Edit it on the Parameters tab.": "还没有备注。可在参数页编辑。",
    "Project": "项目",
    "{0} groups": "{0} 个分组",
    "no note — edit below": "无备注——在下方编辑",
    "No note yet.": "还没有备注。",
    "Address": "地址",
    "MCU": "MCU",
    "Kind": "类型",
    "Address source": "地址来源",
    "Image requires": "镜像要求",
    "Note": "备注",
    "{0} Add entry (or drop bin files here)": "{0} 添加条目（或将 bin 文件拖到此处）",
    "Files…": "文件…",
    "Attachment…": "附件…",
    "Entries come from the build directory": "条目来自构建目录",
    "To add files, archive this version (⋯ menu) or create a fixed firmware": (
        "要添加文件，请归档此版本（⋯ 菜单）或新建固定型固件"
    ),
    "Merge to .bin…": "合成为 .bin…",
    "For {0}; the board is {1}": "适用于 {0}；设备是 {1}",
    "address": "地址",
    "Hex address. Clear the field and confirm to restore the detected address": (
        "十六进制地址。清空并确认即可恢复识别出的地址"
    ),
    "add note": "添加备注",
    "auto ({0})": "自动（{0}）",
    "This file names no chip: choose the MCU it is for. Left empty, it follows the firmware's only chip; with several chips it is not flashed.": (
        "此文件不含芯片信息：请选择它适用的 MCU。留空时跟随固件唯一的芯片；固件有多种芯片时不会烧录它。"
    ),
    "added automatically": "自动添加",
    "placed by partition table": "已按分区表调整",
    "address differs": "地址不符",
    "data/archive/{0}.bin, original name {1}": "data/archive/{0}.bin，原文件名 {1}",
    "0xFF bytes, generated when flashing": "0xFF 字节，烧录时生成",
    "chip {0}{1}{2}": "chip {0}{1}{2}",
    "not set": "未设置",
    "Add entries": "添加条目",
    "Binary files (*.bin);;All files (*)": "二进制文件 (*.bin);;所有文件 (*)",
    "no address": "没有地址",
    "Add attachment": "添加附件",
    "All files (*)": "所有文件 (*)",
    "Partition: {0}": "分区：{0}",
    "At an address…": "指定地址…",
    "Place attachment": "放置附件",
    "Placement": "放置方式",
    "Hex address": "十六进制地址",
    "Re-locate…": "重新定位…",
    "Archive this file": "归档此文件",
    "Copy to firmware…": "复制到固件…",
    "Remove entry": "移除条目",
    "Comes from the build output; untick it to leave it out": "来自构建产物；取消勾选即可不烧录",
    "Re-locate {0}": "重新定位 {0}",
    "Copy entry": "复制条目",
    "Copy {0} to": "将 {0} 复制到",
    "unique within the project, or among standalone firmware": "在项目内唯一，或在独立固件中唯一",
    "from the entries: MCU column on the Entries tab": "由条目决定：见条目页的 MCU 列",
    "Arduino: boot_app0.bin comes from this package; empty uses the Settings path, else auto-detection": (
        "Arduino：boot_app0.bin 取自此框架包；留空时用设置中的路径，再没有则自动检测"
    ),
    "Flash mode / freq / size": "flash 模式 / 频率 / 容量",
    "Reset before": "烧录前复位",
    "saved setting; may be adjusted to the connection type, see Checks": (
        "保存的设置；可能按连接类型调整，见“检查”"
    ),
    "Reset after": "烧录后复位",
    "— (standalone firmware)": "—（独立固件）",
    "change via the ⋯ menu → Move, or drag in the sidebar": "可通过 ⋯ 菜单 → 移动，或在侧栏中拖动来修改",
    "What this firmware is, which board revision, why it exists…": "固件的用途、对应的设备版本、保留原因…",
    "shown in grey next to the name; saved when the field loses focus": "以灰色显示在名称旁；输入框失去焦点时保存",
    "comma-separated, e.g. release, s3": "用逗号分隔，例如 release, s3",
    "Tags": "标签",
    "Fixed snapshot: files are referenced by content hash (archive:<sha256>), the original path is no longer needed.": (
        "固定快照：文件按内容 hash 引用（archive:<sha256>），不再需要原路径。"
    ),
    "Entry names are the recorded original file names; notes are editable.": (
        "条目名为记录的原始文件名；备注可以修改。"
    ),
    "Archived from": "归档自",
    "Re-locate folder…": "重新定位文件夹…",
    "Point every file at the same place in another folder": "将每个文件改为指向另一文件夹中的相同位置",
    "fixed firmware: files must be in this folder or below it; entries are recorded by name, path and hash": (
        "固定型固件：文件必须在此文件夹或其子文件夹内；条目按名称、路径与 hash 记录"
    ),
    "Files folder": "文件所在文件夹",
    "Re-locate folder": "重新定位文件夹",
    "Change…": "更改…",
    "not found": "不存在",
    "exists": "存在",
    "last resolved {0}": "上次解析 {0}",
    "{0} target {1}": "{0} 目标 {1}",
    "Settings path: {0}": "设置中的路径：{0}",
    "auto-detected: {0}": "自动检测：{0}",
    "the Settings path / auto-detection": "设置中的路径 / 自动检测",
    "keep = leave the bootloader header as built": "keep = 保留 bootloader 镜像头中构建时的值",
    "Click a record to show its section of that day's log (logs/YYYY-MM-DD.log).": (
        "点击记录可显示当天日志中的对应段落（logs/YYYY-MM-DD.log）。"
    ),
    "No flash records yet.": "还没有烧录记录。",
    "Entries": "条目",
    "Parameters": "参数",
    "Records": "记录",
    "Files missing: {0}. Re-locate them (right-click an entry) or remove the entries; flashing is blocked until then.": (
        "文件缺失：{0}。请重新定位（右键点击条目）或删除这些条目；在此之前不能烧录。"
    ),
    "Files changed on disk since the last check: {0} (content). Flashing is blocked until you confirm the list below.": (
        "上次核对后磁盘上的文件已变化：{0}（内容）。确认下面的列表之前不能烧录。"
    ),
    "Confirm entries": "确认条目",
    "Rebuild the env in PlatformIO": "请在 PlatformIO 中重新编译该 env",
    "Run <code>idf.py build</code>": "请运行 <code>idf.py build</code>",
    "Target switched, rebuild needed: the configuration targets <b>{0}</b>, the build output is still <b>{1}</b>. {2}; flashing, archiving and merging are blocked until then.": (
        "目标已切换，需要重新编译：配置的目标是 <b>{0}</b>，构建产物仍是 <b>{1}</b>。{2}；"
        "在此之前不能烧录、归档或合成。"
    ),
    "Build incomplete: {0}. Wait for the build to finish or rebuild; flashing is blocked.": (
        "构建未完成：{0}。请等待构建结束或重新编译；暂时不能烧录。"
    ),
    "The build output is being written; checking again shortly.": "构建产物正在写入；稍后会再次核对。",
    "{0}. Last resolved {1}; the entries below are that snapshot and cannot be flashed until the directory exists again.": (
        "{0}。上次解析于 {1}；下面的条目是那次的快照，目录恢复之前不能烧录。"
    ),
    "Change directory…": "更改目录…",
    "<br>The device group may need adjusting.": "<br>设备分组可能需要调整。",
    "The build output changed since it was last confirmed:{0}{1}{2}<br>Flashing is blocked until you confirm.": (
        "构建产物在上次确认后已变化：{0}{1}{2}<br>确认之前不能烧录。"
    ),
    "{0} entries, {1} s": "{0} 个条目，{1} 秒",
    " · {0} already in flash": " · {0} 个已在 flash 中",
    " · after={0}": " · after={0}",
    "partition {0}": "分区 {0}",
    "derived": "derived",
    # .embr export and import (M8)
    "written by a newer Embr": "由更新版本的 Embr 写入",
    "changes need confirmation": "有变化需要确认",
    "the target was switched; rebuild the project": "目标芯片已切换，需重新编译工程",
    "the build is incomplete": "构建不完整",
    "the build output or a file is missing": "构建产物或文件缺失",
    "the build is still being written": "构建产物仍在写入",
    "{0} is not an Embr package ({1})": "{0} 不是 Embr 包（{1}）",
    "{0} appears more than once in the package": "{0} 在包中重复出现",
    "{0} is missing from the package": "包中缺少 {0}",
    "{0}: missing or invalid format_version": "{0}：format_version 缺失或无效",
    "package format {0} is newer than this Embr supports; update Embr": "包格式版本 {0} 高于此版本 Embr 所支持的版本，请升级 Embr",
    "package format {0} is not supported": "不支持包格式版本 {0}",
    "{0}: missing {1}": "{0}：缺少 {1}",
    "{0}: invalid {1}": "{0}：{1} 无效",
    "{0}: a project cannot belong to a project": "{0}：项目不能属于另一个项目",
    "{0}: invalid sha256 in files": "{0}：files 中的 sha256 无效",
    "{0}: invalid size for {1}": "{0}：{1} 的大小无效",
    "{0}: a project is listed twice": "{0}：项目重复列出",
    "{0}: a firmware is listed twice": "{0}：固件重复列出",
    "{0}: a file is listed twice": "{0}：文件重复列出",
    "{0}: invalid item in {1}": "{0}：{1} 中有无效条目",
    "{0}: invalid project_id in {1}": "{0}：{1} 中的 project_id 无效",
    "{0}: its document is missing from the package": "{0}：包中缺少其文件",
    "{0} was written by a newer Embr ({1})": "{0} 由更新版本的 Embr 写入（{1}）",
    "{0}: id does not match the manifest": "{0}：id 与清单不一致",
    "{0}: invalid value ({1})": "{0}：取值无效（{1}）",
    "{0}: its project does not match the manifest": "{0}：所属项目与清单不一致",
    "{0}: its project is not in the package": "{0}：所属项目不在包中",
    "{0}: a tracking firmware cannot come from a package": "{0}：包中不能含有跟随型固件",
    "{0}: {1} refers to a file not in the package": "{0}：{1} 引用的文件不在包中",
    "{0}: {1} does not match its file": "{0}：{1} 与其文件不一致",
    "{0}: missing from the package": "{0}：包中缺少此文件",
    "{0}: size does not match the manifest": "{0}：大小与清单不一致",
    "{0}: cannot be read from the package ({1})": "{0}：无法从包中读取（{1}）",
    "{0}: content does not match the manifest": "{0}：内容与清单不一致",
}
