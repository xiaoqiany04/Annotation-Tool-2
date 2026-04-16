import gradio as gr
import os
import json
import time
import socket
import threading

# ================= 0. 底层 JS 同步脚本 =================
custom_head = """
<script>
let last_idx = -1; 
function highlight_gallery() {
    const slider_wrap = document.getElementById('frame_slider');
    if (!slider_wrap) return;
    const slider = slider_wrap.querySelector('input[type="range"]');
    if (!slider) return;
    const idx = parseInt(slider.value);
    if (isNaN(idx)) return;
    
    const gal = document.getElementById('my_gallery');
    if (!gal) return;
    
    const buttons = gal.querySelectorAll('button');
    let img_buttons = [];
    buttons.forEach(b => {
        if(b.querySelector('img') || b.classList.contains('gallery-item') || b.classList.contains('thumbnail-item')) {
            img_buttons.push(b);
        }
    });
    
    if (img_buttons.length > idx) {
        img_buttons.forEach((btn, i) => {
            if(i === idx) {
                btn.style.outline = '4px solid rgb(231, 76, 60)';
                btn.style.outlineOffset = '-4px';
                if (idx !== last_idx) {
                    btn.scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'center'});
                    last_idx = idx; 
                }
            } else {
                btn.style.outline = 'none';
            }
        });
    }
}
window.addEventListener('load', () => {
    setInterval(highlight_gallery, 100);
});
</script>
"""

# ================= 1. 基础配置与全局状态 =================
# 移除 argparse，因为路径将由用户在 UI 中输入
ACTION_CANDIDATES = [
    "闭眼睡觉", "携带宠物", "拍打后排平板", "拍中央扶手后部", 
    "打电话", "向后探身取物", "脱衣服", "拍打车顶", "电脑办公", 
    "靠近车门手向过道挥动", "靠近过道手向车门挥动", "无人员", "无动作", "其他"
]

save_lock = threading.Lock()

# 动态保存当前的全局路径
class AppState:
    ROOT_FOLDER = ""
    JSONL_FILE_PATH = ""
    OUTPUT_FILE_PATH = ""
    pre_data_map = {}
    saved_records_map = {}
    matched_folders = []

app_state = AppState()

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

# ================= 2. 数据处理辅助函数 =================
def init_data(img_dir, input_jsonl, output_jsonl):
    """根据用户输入的路径初始化数据"""
    if not os.path.exists(img_dir): return False, f"错误：找不到图片目录 {img_dir}"
    if not os.path.exists(input_jsonl): return False, f"错误：找不到输入 JSONL {input_jsonl}"
    
    app_state.ROOT_FOLDER = img_dir
    app_state.JSONL_FILE_PATH = input_jsonl
    app_state.OUTPUT_FILE_PATH = output_jsonl
    app_state.pre_data_map.clear()
    app_state.saved_records_map.clear()
    app_state.matched_folders.clear()

    # 1. 读取预标签
    with open(app_state.JSONL_FILE_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    data = json.loads(line)
                    folder_id = os.path.basename(data.get("source", ""))
                    if folder_id: app_state.pre_data_map[folder_id] = data
                except: pass

    # 2. 读取已保存记录
    if os.path.exists(app_state.OUTPUT_FILE_PATH):
        with open(app_state.OUTPUT_FILE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        old_data = json.loads(line)
                        old_id = os.path.basename(old_data.get("source", ""))
                        if old_id: app_state.saved_records_map[old_id] = old_data
                    except: pass

    # 3. 匹配文件夹
    for item in os.listdir(app_state.ROOT_FOLDER):
        item_path = os.path.join(app_state.ROOT_FOLDER, item)
        if os.path.isdir(item_path) and item in app_state.pre_data_map:
            app_state.matched_folders.append(item)
    app_state.matched_folders.sort()

    if not app_state.matched_folders:
        return False, "错误：在图片目录下没有找到与预标签 JSONL 匹配的子文件夹！"

    return True, f"加载成功！共匹配到 {len(app_state.matched_folders)} 个任务。"

def get_images_from_folder(folder_name):
    if not folder_name: return [], []
    path = os.path.join(app_state.ROOT_FOLDER, folder_name)
    if not os.path.exists(path): return [], []
    files = sorted([f for f in os.listdir(path) if f.lower().endswith(('.png', '.jpg', '.jpeg'))])
    paths = [os.path.join(path, f) for f in files]
    return files, paths

def format_labels_to_html(label_data):
    if not label_data: return "暂无参考标签"
    seat_map = {"FIRST_LEFT": "副驾", "FIRST_RIGHT": "主驾"}
    html_parts = []
    for key, seat_name in seat_map.items():
        seat_info = label_data.get(key, {})
        actions = seat_info.get("action", [])
        actions_str = "、".join(actions) if actions else "无"
        part = (f"<span style='margin-right: 25px; font-size: 16px;'>"
                f"<strong style='color: #2980B9;'>{seat_name}：</strong>"
                f"<span style='background-color: #F8F9F9; padding: 4px 10px; border-radius: 6px; color: #C0392B; font-weight: bold; border: 1px solid #E5E7E9;'>{actions_str}</span></span>")
        html_parts.append(part)
    return f"<div style='padding: 15px; border: 2px solid #D5DBDB; border-radius: 8px; background-color: #FFFFFF; display: flex; align-items: center; flex-wrap: wrap;'><span style='font-size: 16px; font-weight: bold; margin-right: 15px; color: #34495E;'>💡 已有标签 (供参考):</span>{''.join(html_parts)}</div>"

# ================= 3. 构建 Gradio 界面 =================
with gr.Blocks(title="舱内人员行为标注工具", theme=gr.themes.Soft(), head=custom_head) as demo:
    
    # ---------- 【页面 A】：启动配置页 ----------
    with gr.Column(visible=True) as setup_page:
        gr.Markdown("# 🚀 舱内人员行为标注工具 - 启动配置")
        gr.Markdown("请在下方输入分配给您的任务文件夹路径。可以直接复制电脑中的绝对路径粘贴进来。")
        
        in_img_dir = gr.Textbox(label="1. 待标注图片的主目录 (例如: D:\\任务1\\图片集合)", placeholder="粘贴包含很多子文件夹的那个主目录路径")
        in_input_jsonl = gr.Textbox(label="2. 输入的 JSONL 文件路径 (例如: D:\\任务1\\pre.jsonl)", placeholder="粘贴您领到的预标签 jsonl 文件路径")
        in_output_jsonl = gr.Textbox(label="3. 输出的 JSONL 文件路径 (例如: D:\\任务1\\result.jsonl)", placeholder="粘贴您想把结果保存到哪里的完整路径 (如果文件不存在会自动创建)")
        
        start_btn = gr.Button("✅ 确认无误，进入标注系统", variant="primary", size="lg")
        setup_log = gr.Textbox(label="系统检测日志", interactive=False)

    # ---------- 【页面 B】：核心工作页 ----------
    with gr.Column(visible=False) as work_page:
        header_md = gr.Markdown("### 📂 加载中...")
        
        with gr.Row():
            prev_btn = gr.Button("⬅️ 上一条", size="sm", scale=1)
            folder_dropdown = gr.Dropdown(choices=[], label="🔽 选择当前标注的数据文件夹", interactive=True, scale=4)
            next_btn = gr.Button("下一条 ➡️", size="sm", scale=1)
            
            jump_input = gr.Number(label="进度/跳转", value=1, precision=0, interactive=True, scale=1)
            info_text = gr.Textbox(label="当前文件夹帧数", value="", interactive=False, scale=1)

        current_index = gr.State(0)
        current_image_files = gr.State([])
        current_image_paths = gr.State([])

        with gr.Row():
            with gr.Column(scale=1):
                main_image = gr.Image(label="当前帧", interactive=False, height=350)
                with gr.Row():
                    with gr.Column(scale=1, min_width=100):
                        play_btn = gr.Button("▶️ 顺序播放", variant="primary")
                        pause_btn = gr.Button("⏸️ 暂停播放", variant="secondary")
                    slider = gr.Slider(minimum=0, maximum=100, step=1, value=0, label="拖动浏览帧", scale=4, elem_id="frame_slider")

            with gr.Column(scale=1):
                gallery = gr.Gallery(columns=6, height=350, allow_preview=False, label="所有图片一览 (带帧数说明，点击可直接跳转)", elem_id="my_gallery")

        labels_html = gr.HTML(value="加载中...")
        gr.Markdown("---")
        
        with gr.Row():
            with gr.Column(scale=5):
                gr.Markdown("### 1. 分座位：动作起止帧标注")
                def make_seat_ui(title):
                    with gr.Column(variant="panel"):
                        gr.Markdown(f"**{title}**")
                        with gr.Row(equal_height=True):
                            btn_s = gr.Button("设为【开始】", size="sm", scale=2, min_width=60)
                            btn_s_c = gr.Button("❌", size="sm", scale=0, min_width=25)
                            txt_s = gr.Textbox(label="", placeholder="未设置", interactive=False, container=False, scale=3, min_width=60)
                            state_s = gr.State(None)
                        with gr.Row(equal_height=True):
                            btn_e = gr.Button("设为【结束】", size="sm", scale=2, min_width=60)
                            btn_e_c = gr.Button("❌", size="sm", scale=0, min_width=25)
                            txt_e = gr.Textbox(label="", placeholder="未设置", interactive=False, container=False, scale=3, min_width=60)
                            state_e = gr.State(None)
                    return btn_s, btn_s_c, txt_s, state_s, btn_e, btn_e_c, txt_e, state_e

                with gr.Row():
                    fl_btn_s, fl_btn_s_c, fl_txt_s, fl_state_s, fl_btn_e, fl_btn_e_c, fl_txt_e, fl_state_e = make_seat_ui("副驾 (FIRST_LEFT)")
                    fr_btn_s, fr_btn_s_c, fr_txt_s, fr_state_s, fr_btn_e, fr_btn_e_c, fr_txt_e, fr_state_e = make_seat_ui("主驾 (FIRST_RIGHT)")

            with gr.Column(scale=4):
                gr.Markdown("### 2. 质量与标签判断 (⚠️ 必选项)")
                usable_radio = gr.Radio(choices=["可用", "不可用"], value=None, label="该组图片是否可用？ (必选项)")
                correct_radio = gr.Radio(choices=["正确", "错误"], value=None, label="参考标签是否正确？ (必选项)")

                with gr.Group(visible=False) as correction_group:
                    gr.Markdown("#### 请修正标签 (下拉可多选)")
                    with gr.Row():
                        fl_action = gr.Dropdown(choices=ACTION_CANDIDATES, multiselect=True, interactive=True, label="副驾")
                        fr_action = gr.Dropdown(choices=ACTION_CANDIDATES, multiselect=True, interactive=True, label="主驾")

        gr.Markdown("---")
        export_btn = gr.Button("💾 手动保存 (系统已开启后台实时无感自动保存)", variant="secondary", size="lg")
        export_status = gr.Textbox(label="系统日志")

    # ================= 4. 逻辑绑定 =================
    
    # --- 启动配置逻辑 ---
    def enter_system(img_dir, input_jsonl, output_jsonl):
        # 移除路径两端的引号，防止从 Windows 复制路径时带有双引号导致报错
        img_dir = img_dir.strip('"').strip("'")
        input_jsonl = input_jsonl.strip('"').strip("'")
        output_jsonl = output_jsonl.strip('"').strip("'")
        
        success, msg = init_data(img_dir, input_jsonl, output_jsonl)
        if not success:
            return gr.update(visible=True), gr.update(visible=False), msg, gr.update(), gr.update(), gr.update()
        
        # 成功，隐藏设置页，显示工作页，并更新下拉菜单
        header_text = f"### 📂 图片根目录: `{app_state.ROOT_FOLDER}` | 成功匹配到 {len(app_state.matched_folders)} 个待标注文件夹"
        return (
            gr.update(visible=False), # 隐藏 setup
            gr.update(visible=True),  # 显示 work
            msg,                      # 日志
            gr.update(choices=app_state.matched_folders, value=app_state.matched_folders[0]), # 更新下拉框
            gr.update(label=f"进度/跳转 (共 {len(app_state.matched_folders)} 条)"), # 更新总数提示
            gr.update(value=header_text)
        )

    start_btn.click(
        fn=enter_system, 
        inputs=[in_img_dir, in_input_jsonl, in_output_jsonl], 
        outputs=[setup_page, work_page, setup_log, folder_dropdown, jump_input, header_md]
    )

    # --- 标注逻辑 (全部将 pre_data_map 换成 app_state.pre_data_map 等) ---
    save_inputs = [
        folder_dropdown, usable_radio, correct_radio, current_image_files,
        fl_state_s, fl_state_e, fr_state_s, fr_state_e,
        fl_action, fr_action
    ]

    def auto_export_data(selected_folder, usable, correct, files, fl_s, fl_e, fr_s, fr_e, fl_act, fr_act):
        if not selected_folder: return "⚠️ 未选择有效文件夹，无需保存。"
        if usable is None or correct is None: 
            return "🛑 拦截保存：请务必先选择【该组图片是否可用？】和【参考标签是否正确？】！"
            
        for s, e, name in [(fl_s, fl_e, "副驾"), (fr_s, fr_e, "主驾")]:
            if s is not None and e is not None and s > e: return f"⚠️ 自动保存跳过：【{name}】的开始帧 ({s}) 不能晚于结束帧 ({e})，请修正！"

        result = app_state.pre_data_map[selected_folder].copy()
        
        def get_frame_info(s_idx, e_idx):
            return {
                "start_frame_index": s_idx, "start_frame_name": files[s_idx] if s_idx is not None else None,
                "end_frame_index": e_idx, "end_frame_name": files[e_idx] if e_idx is not None else None
            }

        result["frame_annotations"] = {
            "FIRST_LEFT": get_frame_info(fl_s, fl_e), 
            "FIRST_RIGHT": get_frame_info(fr_s, fr_e)
        }
        result["is_usable"] = (usable == "可用")
        result["is_label_correct"] = (correct == "正确")
        if not result["is_label_correct"]:
            result["corrected_labels"] = {"FIRST_LEFT": {"action": fl_act}, "FIRST_RIGHT": {"action": fr_act}}

        with save_lock:
            app_state.saved_records_map[selected_folder] = result
            with open(app_state.OUTPUT_FILE_PATH, "w", encoding="utf-8") as f:
                for record_data in app_state.saved_records_map.values():
                    f.write(json.dumps(record_data, ensure_ascii=False) + "\n")
            
        return f"⚡ 自动保存成功！[{selected_folder}] | 时间: {time.strftime('%H:%M:%S')}"

    export_btn.click(fn=auto_export_data, inputs=save_inputs, outputs=export_status)
    usable_radio.change(fn=auto_export_data, inputs=save_inputs, outputs=export_status)
    fl_action.change(fn=auto_export_data, inputs=save_inputs, outputs=export_status)
    fr_action.change(fn=auto_export_data, inputs=save_inputs, outputs=export_status)

    def toggle_correction_panel(is_correct, selected_folder):
        if is_correct == "错误":
            saved = app_state.saved_records_map.get(selected_folder)
            if saved and "corrected_labels" in saved:
                c_labels = saved["corrected_labels"]
                act_fl = c_labels.get("FIRST_LEFT", {}).get("action", [])
                act_fr = c_labels.get("FIRST_RIGHT", {}).get("action", [])
            else:
                label_data = app_state.pre_data_map.get(selected_folder, {}).get("label", {})
                act_fl = label_data.get("FIRST_LEFT", {}).get("action", [])
                act_fr = label_data.get("FIRST_RIGHT", {}).get("action", [])
            return (gr.update(visible=True), gr.update(value=act_fl), gr.update(value=act_fr))
        else:
            return gr.update(visible=False), gr.update(), gr.update()

    correct_radio.change(
        fn=toggle_correction_panel, inputs=[correct_radio, folder_dropdown], 
        outputs=[correction_group, fl_action, fr_action]
    ).then(fn=auto_export_data, inputs=save_inputs, outputs=export_status)

    def set_frame(idx, files):
        if idx is None or not files: return "", None
        return f"第 {idx} 张 : {files[idx]}", idx

    for btn, txt, state in [(fl_btn_s, fl_txt_s, fl_state_s), (fl_btn_e, fl_txt_e, fl_state_e), (fr_btn_s, fr_txt_s, fr_state_s), (fr_btn_e, fr_txt_e, fr_state_e)]:
        btn.click(fn=set_frame, inputs=[current_index, current_image_files], outputs=[txt, state]).then(fn=auto_export_data, inputs=save_inputs, outputs=export_status)

    for btn_c, txt, state in [(fl_btn_s_c, fl_txt_s, fl_state_s), (fl_btn_e_c, fl_txt_e, fl_state_e), (fr_btn_s_c, fr_txt_s, fr_state_s), (fr_btn_e_c, fr_txt_e, fr_state_e)]:
        btn_c.click(fn=lambda: ("", None), outputs=[txt, state]).then(fn=auto_export_data, inputs=save_inputs, outputs=export_status)

    # =============== 导航与跳转控制 ===============
    def go_prev(current_folder, usable, correct):
        if usable is None or correct is None: return gr.update() 
        if not app_state.matched_folders: return gr.update()
        try:
            idx = app_state.matched_folders.index(current_folder)
            return gr.update(value=app_state.matched_folders[max(0, idx - 1)])
        except ValueError: return gr.update(value=app_state.matched_folders[0])

    def go_next(current_folder, usable, correct):
        if usable is None or correct is None: return gr.update() 
        if not app_state.matched_folders: return gr.update()
        try:
            idx = app_state.matched_folders.index(current_folder)
            return gr.update(value=app_state.matched_folders[min(len(app_state.matched_folders) - 1, idx + 1)])
        except ValueError: return gr.update(value=app_state.matched_folders[0])

    def go_jump(target_idx, current_folder, usable, correct):
        if usable is None or correct is None: return gr.update() 
        if not app_state.matched_folders or target_idx is None: return gr.update()
        try:
            idx = int(target_idx)
            idx = max(1, min(idx, len(app_state.matched_folders)))
            return gr.update(value=app_state.matched_folders[idx - 1])
        except (ValueError, TypeError): return gr.update()

    prev_btn.click(fn=auto_export_data, inputs=save_inputs, outputs=export_status).then(fn=go_prev, inputs=[folder_dropdown, usable_radio, correct_radio], outputs=folder_dropdown)
    next_btn.click(fn=auto_export_data, inputs=save_inputs, outputs=export_status).then(fn=go_next, inputs=[folder_dropdown, usable_radio, correct_radio], outputs=folder_dropdown)
    jump_input.submit(fn=auto_export_data, inputs=save_inputs, outputs=export_status).then(fn=go_jump, inputs=[jump_input, folder_dropdown, usable_radio, correct_radio], outputs=folder_dropdown)

    def load_folder_data(selected_folder):
        if not selected_folder: return [gr.update()] * 22
        files, paths = get_images_from_folder(selected_folder)
        total = len(paths)
        info = f"共 {total} 帧"
        
        try: current_idx = app_state.matched_folders.index(selected_folder) + 1
        except ValueError: current_idx = 1
        jump_update = gr.update(value=current_idx) # 不用每次改 label
        
        original_data = app_state.pre_data_map.get(selected_folder, {})
        html_str = format_labels_to_html(original_data.get("label", {}))
        gallery_items = [(path, f"第 {i} 帧") for i, path in enumerate(paths)]
        
        record = app_state.saved_records_map.get(selected_folder)
        if record:
            is_usable_val = "可用" if record.get("is_usable") else "不可用"
            is_correct_val = "正确" if record.get("is_label_correct") else "错误"
            show_corr = (is_correct_val == "错误")
            c_labels = record.get("corrected_labels", {})
            act_fl = c_labels.get("FIRST_LEFT", {}).get("action", [])
            act_fr = c_labels.get("FIRST_RIGHT", {}).get("action", [])
            f_ann = record.get("frame_annotations", {})
            def get_f(seat_key):
                s_idx = f_ann.get(seat_key, {}).get("start_frame_index")
                e_idx = f_ann.get(seat_key, {}).get("end_frame_index")
                s_txt = f"第 {s_idx} 张 : {files[s_idx]}" if s_idx is not None else ""
                e_txt = f"第 {e_idx} 张 : {files[e_idx]}" if e_idx is not None else ""
                return s_txt, s_idx, e_txt, e_idx
            fl_ts, fl_ss, fl_te, fl_se = get_f("FIRST_LEFT")
            fr_ts, fr_ss, fr_te, fr_se = get_f("FIRST_RIGHT")
        else:
            is_usable_val = None
            is_correct_val = None 
            show_corr = False
            act_fl, act_fr = [], []
            fl_ts = fl_te = fr_ts = fr_te = ""
            fl_ss = fl_se = fr_ss = fr_se = None
        
        return (
            files, paths, 
            gr.update(value=paths[0] if paths else None), gr.update(value=gallery_items), 
            gr.update(maximum=max(0, total-1), value=0), 0, info, jump_update, html_str,  
            gr.update(value=fl_ts), gr.update(value=fl_te), gr.update(value=fr_ts), gr.update(value=fr_te),
            fl_ss, fl_se, fr_ss, fr_se,
            gr.update(value=is_usable_val), gr.update(value=is_correct_val), gr.update(visible=show_corr),
            gr.update(value=act_fl), gr.update(value=act_fr)
        )

    folder_dropdown.change(
        fn=load_folder_data, inputs=folder_dropdown,
        outputs=[
            current_image_files, current_image_paths, main_image, gallery, slider, current_index, info_text, jump_input, labels_html,
            fl_txt_s, fl_txt_e, fr_txt_s, fr_txt_e,
            fl_state_s, fl_state_e, fr_state_s, fr_state_e,
            usable_radio, correct_radio, correction_group, fl_action, fr_action
        ]
    )

    def play_images(paths, start_idx):
        if not paths: return
        if start_idx >= len(paths) - 1: start_idx = 0
        for i in range(start_idx, len(paths)):
            yield paths[i], i, i
            time.sleep(0.15)

    play_event = play_btn.click(fn=play_images, inputs=[current_image_paths, current_index], outputs=[main_image, slider, current_index])
    pause_btn.click(fn=lambda: None, cancels=[play_event])
    slider.change(fn=lambda idx, paths: (paths[idx], idx) if paths and idx < len(paths) else (None, idx), inputs=[slider, current_image_paths], outputs=[main_image, current_index])
    gallery.select(fn=lambda evt, paths: (paths[evt.index], evt.index, evt.index) if paths and evt.index < len(paths) else (None, evt.index, evt.index), inputs=current_image_paths, outputs=[main_image, slider, current_index])


if __name__ == "__main__":
    dynamic_port = get_free_port()
    print(f"🚀 准备启动服务，本地访问: http://127.0.0.1:{dynamic_port}")
    # ⚠️ 为了能读取任何用户填写的路径，我们放开 allowed_paths 限制
    demo.launch(inbrowser=True, server_port=dynamic_port, server_name="0.0.0.0", allowed_paths=["/"])