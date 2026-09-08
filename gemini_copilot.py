bl_info = {
    "name": "Gemini Blender Copilot",
    "author": "AI Assistant",
    "version": (2, 5),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Gemini Copilot",
    "description": "透過 Gemini API 自動生成並執行 Blender Python 腳本 (支援思考標籤過濾、語法先行檢驗、常用模組自動注入、503 自動容災與 Undo 復原)",
    "category": "Development",
}

import bpy
import urllib.request
import urllib.error
import json
import re
import time
import threading
import ast
import math
import bmesh
import mathutils
import random

addon_name = __package__ if __package__ else (__name__ if __name__ != "__main__" else "gemini_blender_copilot")

# 全域狀態控制：防連點冷卻、背景請求狀態與本地快取
_request_state = {
    "is_running": False,
    "last_request_time": 0.0,
    "result": None,
}

_prompt_cache = {}  # prompt_text.strip() -> generated_code
COOLDOWN_SECONDS = 3.0

# 嚴謹且專注於純物件建模的系統指令 (防止 AI 隨意清空場景或生成無關環境，強調 Blender 5.x Collection API)
SYSTEM_INSTRUCTION = (
    "You are an expert Blender 5.x Python developer.\n"
    "Your goal is to write high-quality, clean, valid, and executable Blender Python code based on the user's prompt.\n\n"
    "CRITICAL RULES:\n"
    "1. OUTPUT ONLY EXECUTABLE PYTHON CODE wrapped inside markdown backticks: ```python ... ```. "
    "Do NOT output any conversational text, thoughts, reasoning steps, or notes outside the markdown code block.\n"
    "2. CREATE ONLY THE REQUESTED OBJECT(S): If the user asks for 'a Boeing 747' (波音747) or 'a pyramid' (金字塔), create ONLY that object. "
    "NEVER create giant ground planes, desert floors, landscapes, terrain planes, extra cameras, or studio backdrops unless the user explicitly requested them.\n"
    "3. NEVER DELETE EXISTING SCENE OBJECTS: Do NOT call bpy.ops.object.delete() or select_all(action='SELECT'). Always add objects directly to the existing scene.\n"
    "4. STANDARD SCALE & PLACEMENT: Place the object centered at (0, 0, 0) or standing upright on the ground plane (z >= 0) with a standard visible size (length/height around 2 to 6 units).\n"
    "5. ACTIVE SELECTION: Ensure the newly created object is selected and set as the active object:\n"
    "   obj.select_set(True)\n"
    "   bpy.context.view_layer.objects.active = obj\n"
    "6. BLENDER 5.x COLLECTION API (CRITICAL): If using bpy.data.objects.new(), pass object_data positionally or with object_data= (NOT mesh=), "
    "and link objects using `bpy.context.collection.objects.link(obj)`. NEVER use obsolete 2.7x APIs like `bpy.context.scene.objects.link(obj)`.\n"
    "7. COMPLETE CODE: Ensure all functions defined in the script are invoked at the end of the script."
)

# 支援的 Google AI Studio 最新標準模型清單
AVAILABLE_MODELS = [
    ('gemini-3.6-flash', 'Gemini 3.6 Flash (官方主力推薦，最快最強)', 'Google 官方推薦最新主力模型，速度極快、代碼品質頂尖且支援免費層'),
    ('gemini-3.5-flash-lite', 'Gemini 3.5 Flash-Lite (極速，最省額度)', '超低延遲且資源佔用最低，最省配額且極穩定'),
    ('gemini-flash-latest', 'Gemini Flash Latest (最新 Flash 自動對齊)', '自動對齊 Google 最新發布之 Flash 模型版本'),
    ('gemini-flash-lite-latest', 'Gemini Flash-Lite Latest (最新輕量版)', '自動對齊 Google 最新發布之輕量 Flash 模型'),
    ('gemini-3.1-flash-lite', 'Gemini 3.1 Flash-Lite (輕量穩定)', '穩定的輕量化代碼生成模型'),
    ('CUSTOM', '自訂模型名稱 (Custom Model)', '手動輸入特定模型 ID'),
]

# 常用離線範本庫 (0 Token 消耗，免聯網)
PRESET_SCRIPTS = {
    'cube_grid': {
        'name': "彩色方塊矩陣 (5x5x5 Cube Grid)",
        'prompt': "建立一個由 5x5x5 個彩色方塊組成的矩陣，每個方塊都有隨機材質顏色",
        'code': '''import bpy
import random

col_name = "Gemini_CubeGrid"
col = bpy.data.collections.get(col_name)
if not col:
    col = bpy.data.collections.new(col_name)
    bpy.context.scene.collection.children.link(col)

size = 5
spacing = 1.6

for x in range(size):
    for y in range(size):
        for z in range(size):
            loc = ((x - size / 2) * spacing, (y - size / 2) * spacing, (z - size / 2) * spacing)
            bpy.ops.mesh.primitive_cube_add(size=0.8, location=loc)
            cube = bpy.context.active_object
            
            mat = bpy.data.materials.new(name=f"Mat_{x}_{y}_{z}")
            mat.use_nodes = True
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            if bsdf:
                r, g, b = random.random(), random.random(), random.random()
                bsdf.inputs['Base Color'].default_value = (r, g, b, 1.0)
                bsdf.inputs['Roughness'].default_value = 0.2
            cube.data.materials.append(mat)
            
            for c in list(cube.users_collection):
                c.objects.unlink(cube)
            col.objects.link(cube)
'''
    },
    'studio_lighting': {
        'name': "攝影棚三點燈光與相機 (Studio Setup)",
        'prompt': "建立攝影棚三點光源 (主光、輔光、輪廓光) 與正對場景中心的相機",
        'code': '''import bpy
import math

cam_data = bpy.data.cameras.new("Studio_Camera")
cam_obj = bpy.data.objects.new("Studio_Camera", cam_data)
bpy.context.scene.collection.objects.link(cam_obj)
cam_obj.location = (0, -8, 4)
cam_obj.rotation_euler = (math.radians(65), 0, 0)
bpy.context.scene.camera = cam_obj

key_light = bpy.data.lights.new("Key_Light", type='AREA')
key_light.energy = 800
key_light.size = 2.5
key_obj = bpy.data.objects.new("Key_Light", key_light)
bpy.context.scene.collection.objects.link(key_obj)
key_obj.location = (4.5, -4, 5)
key_obj.rotation_euler = (math.radians(45), math.radians(15), math.radians(45))

fill_light = bpy.data.lights.new("Fill_Light", type='AREA')
fill_light.energy = 300
fill_light.size = 3.5
fill_obj = bpy.data.objects.new("Fill_Light", fill_light)
bpy.context.scene.collection.objects.link(fill_obj)
fill_obj.location = (-4.5, -3, 3.5)
fill_obj.rotation_euler = (math.radians(50), math.radians(-15), math.radians(-45))

rim_light = bpy.data.lights.new("Rim_Light", type='SUN')
rim_light.energy = 3.5
rim_obj = bpy.data.objects.new("Rim_Light", rim_light)
bpy.context.scene.collection.objects.link(rim_obj)
rim_obj.location = (0, 5, 6)
rim_obj.rotation_euler = (math.radians(-45), 0, 0)
'''
    },
    'clean_meshes': {
        'name': "清理所有網格物體 (Clean All Meshes)",
        'prompt': "安全清除場景中所有 MESH 幾何物件並釋放孤立材質",
        'code': '''import bpy

for obj in list(bpy.data.objects):
    if obj.type == 'MESH':
        bpy.data.objects.remove(obj, do_unlink=True)

for mesh in list(bpy.data.meshes):
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)
for mat in list(bpy.data.materials):
    if mat.users == 0:
        bpy.data.materials.remove(mat)
'''
    },
    'pbr_gold': {
        'name': "程序化金屬材質球 (PBR Gold Sphere)",
        'prompt': "在場景中心建立高細分圓球並賦予真實黃金 PBR 金屬材質",
        'code': '''import bpy

bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1.5, location=(0, 0, 1.5))
sphere = bpy.context.active_object
bpy.ops.object.shade_smooth()

mat = bpy.data.materials.new(name="Gold_PBR")
mat.use_nodes = True
bsdf = mat.node_tree.nodes.get("Principled BSDF")
if bsdf:
    bsdf.inputs['Base Color'].default_value = (1.0, 0.766, 0.336, 1.0)
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Roughness'].default_value = 0.15

sphere.data.materials.append(mat)
'''
    }
}

PRESET_ENUM_ITEMS = [
    (key, data['name'], data['prompt']) for key, data in PRESET_SCRIPTS.items()
]


def install_compatibility_polyfills():
    """為 Blender 安裝向下相容補丁，徹底消除 AI 幻覺舊版 API (如 scene.objects.link / collection.link) 引起的崩潰"""
    try:
        def scene_objs_link(*args):
            obj = args[1] if len(args) > 1 else args[0]
            if obj and hasattr(obj, "name") and obj.name not in bpy.context.collection.objects:
                bpy.context.collection.objects.link(obj)
            return obj

        def scene_objs_unlink(*args):
            obj = args[1] if len(args) > 1 else args[0]
            if obj and hasattr(obj, "users_collection"):
                for c in list(obj.users_collection):
                    c.objects.unlink(obj)

        if hasattr(bpy.types, 'SceneObjects'):
            bpy.types.SceneObjects.link = scene_objs_link
            bpy.types.SceneObjects.unlink = scene_objs_unlink

        def col_link(self, item):
            if isinstance(item, bpy.types.Object):
                if item.name not in self.objects:
                    self.objects.link(item)
            elif isinstance(item, bpy.types.Collection):
                if item.name not in self.children:
                    self.children.link(item)
            return item

        def col_unlink(self, item):
            if isinstance(item, bpy.types.Object):
                if item.name in self.objects:
                    self.objects.unlink(item)
            elif isinstance(item, bpy.types.Collection):
                if item.name in self.children:
                    self.children.unlink(item)

        if hasattr(bpy.types, 'Collection'):
            bpy.types.Collection.link = col_link
            bpy.types.Collection.unlink = col_unlink

        # BMesh 常用算子相容補丁 (消除 AI 呼叫 create_cylinder / create_sphere / create_plane 引起的算子不存在報錯)
        if hasattr(bmesh, 'ops'):
            if not hasattr(bmesh.ops, 'create_cylinder'):
                def bmesh_create_cylinder(bm, **kwargs):
                    r = kwargs.pop('radius', kwargs.pop('radius1', 1.0))
                    r1 = kwargs.pop('radius1', r)
                    r2 = kwargs.pop('radius2', r)
                    depth = kwargs.pop('depth', 2.0)
                    cap_ends = kwargs.pop('cap_ends', True)
                    segments = kwargs.pop('segments', 32)
                    return bmesh.ops.create_cone(
                        bm,
                        cap_ends=cap_ends,
                        segments=segments,
                        radius1=r1,
                        radius2=r2,
                        depth=depth,
                        **kwargs
                    )
                setattr(bmesh.ops, 'create_cylinder', bmesh_create_cylinder)

            if not hasattr(bmesh.ops, 'create_sphere'):
                def bmesh_create_sphere(bm, **kwargs):
                    r = kwargs.pop('radius', 1.0)
                    u_segments = kwargs.pop('u_segments', kwargs.pop('segments', 32))
                    v_segments = kwargs.pop('v_segments', kwargs.pop('ring_count', 16))
                    return bmesh.ops.create_uvsphere(
                        bm,
                        u_segments=u_segments,
                        v_segments=v_segments,
                        radius=r,
                        **kwargs
                    )
                setattr(bmesh.ops, 'create_sphere', bmesh_create_sphere)

            if not hasattr(bmesh.ops, 'create_plane'):
                def bmesh_create_plane(bm, **kwargs):
                    size = kwargs.pop('size', 2.0)
                    x_segments = kwargs.pop('x_segments', 1)
                    y_segments = kwargs.pop('y_segments', 1)
                    return bmesh.ops.create_grid(
                        bm,
                        x_segments=x_segments,
                        y_segments=y_segments,
                        size=size,
                        **kwargs
                    )
                setattr(bmesh.ops, 'create_plane', bmesh_create_plane)
    except Exception as e:
        print("[Gemini Copilot] Polyfill warning:", e)


def sanitize_code_for_blender5(code):
    """自動修復 AI 生成代碼中常見的舊版 Blender API 語法錯誤"""
    # 1. 修復 bpy.data.objects.new(name="...", mesh=mesh) -> object_data=mesh
    code = re.sub(
        r'(\bbpy\s*\.\s*data\s*\.\s*objects\s*\.\s*new\s*\([^)]*?),\s*mesh\s*=',
        r'\1, object_data=',
        code
    )
    # 2. 修復 scene.objects.link(obj) -> context.collection.objects.link(obj)
    code = re.sub(
        r'(?:bpy\s*\.\s*context\s*\.\s*)?scene\s*\.\s*objects\s*\.\s*link\s*\(',
        'bpy.context.collection.objects.link(',
        code
    )
    # 3. 修復 collection.link(obj) -> collection.objects.link(obj)
    code = re.sub(
        r'(\bcollection)\s*\.\s*link\s*\(',
        r'\1.objects.link(',
        code
    )
    return code


def extract_python_code(text):
    """強健提取 Python 代碼，徹底移除 Markdown 標記與外部思考/說明文字"""
    text = text.strip()
    if not text:
        return ""

    # 1. 完整閉合的 Markdown 區塊：```python ... ``` 或 ```py ... ``` 或 ``` ... ```
    m = re.search(r"```(?:python|py)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if m:
        extracted = m.group(1).strip()
    else:
        # 2. 未閉合的代碼區塊 (例如因長度截斷，開頭有 ```python 但結尾缺少 ```)
        m = re.search(r"```(?:python|py)?\s*\n(.*)", text, re.DOTALL | re.IGNORECASE)
        if m:
            cleaned = m.group(1).strip()
            extracted = re.sub(r"```+$", "", cleaned).strip()
        else:
            # 3. 備用過濾：如果沒有 markdown 標籤，僅提取包含 Python 關鍵字的區塊
            lines = text.splitlines()
            code_lines = []
            in_code = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("```"):
                    continue
                if stripped.startswith("import ") or stripped.startswith("def ") or stripped.startswith("bpy.") or stripped.startswith("from "):
                    in_code = True
                if in_code:
                    code_lines.append(line)
            extracted = "\n".join(code_lines).strip() if code_lines else "\n".join(lines).strip()

    return sanitize_code_for_blender5(extracted)


def focus_3d_viewport_on_selected():
    """自動解除 Local View 隔離並將 3D 視圖鏡頭平滑對焦置中於選取的物件上"""
    if not bpy.context.window_manager:
        return
    for window in bpy.context.window_manager.windows:
        if not window.screen:
            continue
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D' and space.local_view:
                        try:
                            with bpy.context.temp_override(window=window, area=area):
                                bpy.ops.view3d.localview()
                        except:
                            pass
                for region in area.regions:
                    if region.type == 'WINDOW':
                        try:
                            with bpy.context.temp_override(window=window, area=area, region=region):
                                bpy.ops.view3d.view_selected()
                        except:
                            pass


# 1. 全域偏好設定
class GeminiCopilotPreferences(bpy.types.AddonPreferences):
    bl_idname = addon_name

    api_key: bpy.props.StringProperty(
        name="API Key",
        description="輸入 Google Gemini API Key (可於 Google AI Studio 免費取得)",
        default="",
        subtype='PASSWORD'
    )

    model: bpy.props.EnumProperty(
        name="模型版本",
        description="選擇要使用的 Gemini 模型",
        items=AVAILABLE_MODELS,
        default='gemini-3.6-flash'
    )

    custom_model: bpy.props.StringProperty(
        name="自訂模型 ID",
        description="當模型選為自訂時生效 (例如：gemini-3.6-flash)",
        default="gemini-3.6-flash"
    )

    max_tokens: bpy.props.IntProperty(
        name="輸出 Token 上限",
        description="限制單次回傳的最大 Token 數量 (Gemini 3.x 包含推理思考建議 4096 以上)",
        default=8192,
        min=512,
        max=16384
    )

    temperature: bpy.props.FloatProperty(
        name="生成溫度 (Temperature)",
        description="控制 AI 創意度。代碼生成建議 0.1~0.3 以維持高穩定性",
        default=0.2,
        min=0.0,
        max=1.0,
        precision=2
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Gemini Copilot 全域設定", icon='PREFERENCES')
        layout.prop(self, "api_key")
        layout.prop(self, "model")
        if self.model == 'CUSTOM':
            layout.prop(self, "custom_model")
        
        row = layout.row(align=True)
        row.prop(self, "max_tokens")
        row.prop(self, "temperature")


# 2. 場景屬性組 (支援獨立面板即時調整)
class GeminiCopilotSettings(bpy.types.PropertyGroup):
    prompt: bpy.props.StringProperty(
        name="提示詞",
        description="你想讓 AI 建立什麼？",
        default="建立一個波音747"
    )
    status: bpy.props.StringProperty(
        name="狀態",
        default="準備就緒"
    )
    auto_execute: bpy.props.BoolProperty(
        name="生成後自動執行",
        description="若勾選，生成完畢後立即在場景中執行；若取消勾選，僅將代碼寫入文字編輯器供預覽檢驗",
        default=True
    )
    preset_selection: bpy.props.EnumProperty(
        name="離線範本",
        description="選擇內建常用離線腳本 (0 Token 消耗)",
        items=PRESET_ENUM_ITEMS,
        default='cube_grid'
    )

    # 針對以文字編輯器直接運行插件時的臨時備用配置
    api_key_fallback: bpy.props.StringProperty(name="API Key (暫時)", default="", subtype='PASSWORD')
    model_fallback: bpy.props.EnumProperty(name="模型版本 (暫時)", items=AVAILABLE_MODELS, default='gemini-3.6-flash')
    custom_model_fallback: bpy.props.StringProperty(name="自訂模型 (暫時)", default="gemini-3.6-flash")
    max_tokens_fallback: bpy.props.IntProperty(name="Token 上限", default=8192, min=512, max=16384)
    temperature_fallback: bpy.props.FloatProperty(name="溫度", default=0.2, min=0.0, max=1.0, precision=2)


def get_active_config(context):
    prefs = context.preferences.addons.get(addon_name)
    if prefs and prefs.preferences.api_key:
        p = prefs.preferences
        model_id = p.custom_model.strip() if p.model == 'CUSTOM' else p.model
        max_tokens = max(p.max_tokens, 4096)
        return p.api_key.strip(), model_id, max_tokens, p.temperature
    
    settings = context.scene.gemini_settings
    model_id = settings.custom_model_fallback.strip() if settings.model_fallback == 'CUSTOM' else settings.model_fallback
    max_tokens = max(settings.max_tokens_fallback, 4096)
    return settings.api_key_fallback.strip(), model_id, max_tokens, settings.temperature_fallback


def write_code_to_text_editor(code, name="gemini_script.py"):
    """將生成的代碼寫入 Blender 內建文字編輯器，方便使用者隨時檢視與修改"""
    clean_code = extract_python_code(code)
    text_block = bpy.data.texts.get(name)
    if not text_block:
        text_block = bpy.data.texts.new(name)
    text_block.clear()
    text_block.write(clean_code)
    return text_block


def execute_generated_code(code, report_func=None):
    """執行生成的 Python 腳本，自動選取新物件、對焦 3D 視圖並推入 Undo 復原點"""
    clean_code = extract_python_code(code)
    if not clean_code:
        msg = "未能獲取有效的 Python 程式碼"
        if report_func:
            report_func({'ERROR'}, msg)
        return False, msg, []

    # 語法先行驗證，防止非 Python 思考字串或未閉合字串進入 exec
    try:
        ast.parse(clean_code)
    except SyntaxError as syn_err:
        err_msg = f"代碼語法錯誤 (第 {syn_err.lineno} 行): {syn_err.msg}"
        if report_func:
            report_func({'ERROR'}, err_msg)
        return False, err_msg, []

    # 確保相容性 polyfill 處於啟用狀態
    install_compatibility_polyfills()

    try:
        # 若目前處於編輯等非 OBJECT 模式，切換回 OBJECT 模式
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except:
                pass

        # 記錄執行前的物件集合
        existing_objects = set(bpy.data.objects)

        # 推入 Undo 復原點：按下 Ctrl+Z 即可完美復原
        bpy.ops.ed.undo_push(message="Gemini Script Execution")
        
        # 預先注入常用 Blender 模組，杜絕 NameError: name 'mathutils' is not defined
        global_dict = {
            "bpy": bpy,
            "math": math,
            "bmesh": bmesh,
            "mathutils": mathutils,
            "random": random,
            "__builtins__": __builtins__,
        }
        exec(clean_code, global_dict)

        # 檢測新建立的物件
        new_objects = [o for o in bpy.data.objects if o not in existing_objects]

        # 若未檢測到新物件，檢查腳本是否定義了建構函數但忘記在末端呼叫 (例如 def create_boeing_747() 或 main())
        if not new_objects:
            candidate_funcs = [
                v for k, v in global_dict.items()
                if callable(v) and (k.startswith("create_") or k.startswith("build_") or k.startswith("make_") or k in ("main", "generate", "run"))
            ]
            for func in candidate_funcs:
                try:
                    func()
                    new_objects = [o for o in bpy.data.objects if o not in existing_objects]
                    if new_objects:
                        break
                except Exception as fe:
                    print(f"[Gemini Copilot] 自動補呼叫函數 {getattr(func, '__name__', str(func))} 失敗: {fe}")

        # 確保新物件已正確鏈結至場景集合且處於可見狀態
        for obj in new_objects:
            if not obj.users_collection:
                bpy.context.scene.collection.objects.link(obj)
            obj.hide_viewport = False
            obj.hide_set(False)

        # 自動選取新建立的物件，並將其設為主動物件
        if new_objects:
            for obj in bpy.context.selected_objects:
                obj.select_set(False)
            for obj in new_objects:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = new_objects[0]

            # 自動將 3D 視圖鏡頭平滑對焦置中於新物件上
            focus_3d_viewport_on_selected()

        # 刷新視圖圖層與 3D 區域
        if bpy.context.view_layer:
            bpy.context.view_layer.update()
        if bpy.context.window_manager:
            for window in bpy.context.window_manager.windows:
                if window.screen:
                    for area in window.screen.areas:
                        if area.type == 'VIEW_3D':
                            area.tag_redraw()

        if new_objects:
            names_str = ", ".join([o.name for o in new_objects[:3]])
            if len(new_objects) > 3:
                names_str += f" 等共 {len(new_objects)} 個"
            res_msg = f"已建立: {names_str} (視圖已自動對焦)"
        else:
            res_msg = "腳本執行成功，但未建立任何新物件 (請確認腳本邏輯)"

        if report_func:
            report_func({'INFO'}, f"{res_msg} (Ctrl+Z 可復原)")
        return True, res_msg, new_objects

    except Exception as exec_err:
        err_msg = str(exec_err)
        if report_func:
            report_func({'ERROR'}, f"程式碼執行錯誤: {err_msg}")
        return False, f"執行出錯: {err_msg}", []


# 3. 測試 API 連線 Operator
class OBJECT_OT_gemini_test_connection(bpy.types.Operator):
    bl_idname = "object.gemini_test_connection"
    bl_label = "測試 API 連線"
    bl_description = "測試目前設定的 API Key 與模型是否連線正常"

    def execute(self, context):
        settings = context.scene.gemini_settings
        api_key, model_id, _, _ = get_active_config(context)

        if not api_key:
            self.report({'ERROR'}, "請先輸入 Gemini API Key！")
            settings.status = "錯誤：缺少 API Key"
            return {'CANCELLED'}

        settings.status = f"正在測試連線 ({model_id})..."

        test_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 10}
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Blender-Gemini-Copilot/2.5",
        }
        req = urllib.request.Request(test_url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    settings.status = f"連線成功：{model_id} 運作正常"
                    self.report({'INFO'}, f"Gemini API 連線成功！模型 [{model_id}] 正常可用")
                    return {'FINISHED'}
        except urllib.error.HTTPError as http_err:
            try:
                err_data = json.loads(http_err.read().decode('utf-8'))
                err_detail = err_data.get('error', {}).get('message', str(http_err))
            except:
                err_detail = str(http_err)

            if http_err.code == 404:
                msg = f"找不到模型 [{model_id}] (404)。推薦切換為官方主力模型 gemini-3.6-flash"
            elif http_err.code == 429:
                msg = f"模型 [{model_id}] 目前請求頻率受限 (429)。請稍候 15 秒再試或切換至 gemini-3.5-flash-lite"
            elif http_err.code in (400, 401, 403):
                msg = f"API Key 認證失敗 ({http_err.code}): 請檢查 Key 是否正確"
            elif http_err.code == 503:
                msg = f"模型 [{model_id}] 伺服器繁忙 (503)，請稍候再試或切換至 gemini-3.5-flash-lite"
            else:
                msg = f"HTTP 錯誤 ({http_err.code}): {err_detail}"

            settings.status = f"連線失敗: {msg}"
            self.report({'ERROR'}, msg)
        except Exception as e:
            settings.status = f"網路異常: {str(e)}"
            self.report({'ERROR'}, f"連線異常: {str(e)}")

        return {'CANCELLED'}


# 4. 套用離線範本 Operator (0 Token 消耗)
class OBJECT_OT_gemini_load_preset(bpy.types.Operator):
    bl_idname = "object.gemini_load_preset"
    bl_label = "載入離線範本"
    bl_description = "套用內建離線常用腳本 (完全免連線，0 Token 消耗)"

    def execute(self, context):
        settings = context.scene.gemini_settings
        preset_key = settings.preset_selection
        preset = PRESET_SCRIPTS.get(preset_key)

        if not preset:
            self.report({'ERROR'}, "找不到指定範本")
            return {'CANCELLED'}

        settings.prompt = preset['prompt']
        write_code_to_text_editor(preset['code'])

        if settings.auto_execute:
            success, msg, _ = execute_generated_code(preset['code'], self.report)
            if success:
                settings.status = f"離線範本已載入並執行 ({preset['name']})"
            else:
                settings.status = f"範本執行出錯: {msg}"
        else:
            settings.status = f"已將範本寫入文字編輯器 (gemini_script.py)"
            self.report({'INFO'}, "離線範本已寫入文字編輯器，可手動預覽與修改")

        return {'FINISHED'}


# 5. 僅執行文字編輯器中的代碼 Operator
class OBJECT_OT_gemini_run_editor_code(bpy.types.Operator):
    bl_idname = "object.gemini_run_editor_code"
    bl_label = "執行編輯器腳本"
    bl_description = "執行目前文字編輯器中 gemini_script.py 的代碼 (自動對焦新物件，支援 Ctrl+Z 復原)"

    def execute(self, context):
        settings = context.scene.gemini_settings
        text_block = bpy.data.texts.get("gemini_script.py")

        if not text_block or not text_block.as_string().strip():
            self.report({'WARNING'}, "文字編輯器中目前沒有 gemini_script.py 或內容為空")
            return {'CANCELLED'}

        raw_code = text_block.as_string()
        clean_code = extract_python_code(raw_code)
        
        # 若編輯器中代碼有 Markdown 殘留或舊版語法，自動洗淨回寫
        if clean_code != raw_code.strip():
            text_block.clear()
            text_block.write(clean_code)

        success, msg, new_objs = execute_generated_code(clean_code, self.report)
        if success:
            settings.status = f"編輯器代碼: {msg}"
        else:
            settings.status = f"編輯器代碼: {msg}"
        return {'FINISHED'}


# 6. 一鍵對焦目前物件 Operator
class OBJECT_OT_gemini_focus_object(bpy.types.Operator):
    bl_idname = "object.gemini_focus_object"
    bl_label = "視圖對焦物件"
    bl_description = "將 3D 視圖鏡頭平滑對焦置中於選取的物件上"

    def execute(self, context):
        if not context.selected_objects:
            self.report({'WARNING'}, "目前沒有選取任何物件！請先在 Outliner 或 3D 視圖中選取物件")
            return {'CANCELLED'}
        focus_3d_viewport_on_selected()
        self.report({'INFO'}, f"視圖已對焦至: {context.active_object.name if context.active_object else '選取物件'}")
        return {'FINISHED'}


# 7. 清除本地快取 Operator
class OBJECT_OT_gemini_clear_cache(bpy.types.Operator):
    bl_idname = "object.gemini_clear_cache"
    bl_label = "清除本地快取"
    bl_description = "清除目前工作階段儲存的提示詞快取"

    def execute(self, context):
        global _prompt_cache
        count = len(_prompt_cache)
        _prompt_cache.clear()
        context.scene.gemini_settings.status = f"快取已清除 (釋放 {count} 筆)"
        self.report({'INFO'}, f"本地快取已清空 (共釋放 {count} 筆記錄)")
        return {'FINISHED'}


# 8. 背景非同步核心生成 Operator (支援 503 自動降級容災)
def _async_gemini_worker(api_key, primary_model_id, max_tokens, temperature, prompt):
    """背景線程工作函式：負責發送 HTTP 請求、過濾思考字串、代碼解析與 429/503 自動備援容災"""
    models_to_try = [primary_model_id]
    if primary_model_id != 'gemini-3.5-flash-lite':
        models_to_try.append('gemini-3.5-flash-lite')
    if primary_model_id != 'gemini-3.6-flash' and 'gemini-3.6-flash' not in models_to_try:
        models_to_try.append('gemini-3.6-flash')

    for attempt, current_model in enumerate(models_to_try):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens
            }
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Blender-Gemini-Copilot/2.5",
        }

        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=50) as response:
                res_body = response.read().decode('utf-8')
                res_data = json.loads(res_body)

                if 'candidates' not in res_data or not res_data['candidates']:
                    if 'error' in res_data:
                        err_msg = res_data['error'].get('message', '未知 API 錯誤')
                        _request_state["result"] = {"success": False, "error": f"API 錯誤: {err_msg}"}
                    else:
                        _request_state["result"] = {"success": False, "error": "伺服器未回傳有效候選結果"}
                    return

                candidate = res_data['candidates'][0]
                finish_reason = candidate.get('finishReason', 'STOP')

                if finish_reason not in ('STOP', 'MAX_TOKENS'):
                    _request_state["result"] = {"success": False, "error": f"生成受阻: 原因代碼 {finish_reason}"}
                    return

                parts = candidate.get('content', {}).get('parts', [])
                
                # 【核心過濾】：僅選取非思考過程 (thought=False) 的正式回傳，杜絕思考草稿干擾
                content_parts = [p for p in parts if not p.get('thought', False) and 'text' in p]
                
                if not content_parts:
                    # 若所有 parts 均為思考標籤，表示在思考階段即達上限或被中斷
                    _request_state["result"] = {
                        "success": False,
                        "error": "AI 推理超時或達上限，未能產出正式代碼 (請重新發送或改用 Flash-Lite 模型)"
                    }
                    return

                text_response = ''.join(p.get('text', '') for p in content_parts).strip()

                if not text_response:
                    _request_state["result"] = {"success": False, "error": f"回傳資料缺少文字內容 (結束原因: {finish_reason})"}
                    return

                clean_code = extract_python_code(text_response)
                if not clean_code:
                    _request_state["result"] = {"success": False, "error": "未能自回傳內容中解析出有效的 Python 程式碼"}
                    return

                _request_state["result"] = {
                    "success": True,
                    "code": clean_code,
                    "truncated": finish_reason == 'MAX_TOKENS',
                    "model_used": current_model,
                }
                return

        except urllib.error.HTTPError as http_err:
            try:
                err_body = http_err.read().decode('utf-8')
                err_json = json.loads(err_body)
                err_msg = err_json.get('error', {}).get('message', str(http_err))
            except:
                err_msg = str(http_err)

            # 若 503 (伺服器繁忙) 或 429 (請求頻率/配額受限) 且還有備選模型，自動嘗試備援模型
            if http_err.code in (429, 503) and attempt < len(models_to_try) - 1:
                next_model = models_to_try[attempt + 1]
                print(f"[Gemini Copilot] 模型 {current_model} 遇 HTTP {http_err.code}，自動切換至備援模型: {next_model}")
                time.sleep(0.5)
                continue

            if http_err.code == 404:
                reason = f"找不到模型 [{current_model}]。推薦改用 gemini-3.6-flash"
            elif http_err.code == 429:
                reason = f"模型 [{current_model}] 請求頻率過高 (429)。請稍候 15 秒再試或切換至 gemini-3.5-flash-lite"
            elif http_err.code in (400, 401, 403):
                reason = f"API Key 錯誤 ({http_err.code}): {err_msg}"
            elif http_err.code == 503:
                reason = f"伺服器繁忙 (503)，請稍後再試或使用 gemini-3.5-flash-lite"
            else:
                reason = f"HTTP {http_err.code}: {err_msg}"

            _request_state["result"] = {"success": False, "error": reason}
            return

        except Exception as e:
            if attempt < len(models_to_try) - 1:
                time.sleep(0.5)
                continue
            _request_state["result"] = {"success": False, "error": f"網路異常: {str(e)}"}
            return


def _gemini_poll_timer():
    """主執行緒計時器：檢查背景非同步請求是否完成並安全處理 Blender UI"""
    if _request_state["result"] is None:
        return 0.1

    res = _request_state["result"]
    _request_state["is_running"] = False
    _request_state["result"] = None

    context = bpy.context
    settings = context.scene.gemini_settings
    prompt_key = settings.prompt.strip()

    if res.get("success"):
        code = res.get("code", "")
        clean_code = extract_python_code(code)
        
        # 寫入本地快取
        _prompt_cache[prompt_key] = clean_code
        # 同步寫入文字編輯器
        write_code_to_text_editor(clean_code)

        if res.get("truncated"):
            settings.status = "警告：輸出達 Token 上限而截斷"

        if settings.auto_execute:
            success, msg, new_objs = execute_generated_code(clean_code)
            if success:
                settings.status = msg
            else:
                settings.status = f"生成完畢但執行出錯: {msg}"
        else:
            settings.status = "生成完畢！已寫入文字編輯器 (可手動執行)"
    else:
        err = res.get("error", "未知錯誤")
        settings.status = f"錯誤: {err}"

    if bpy.context and bpy.context.window_manager:
        for window in bpy.context.window_manager.windows:
            if window.screen:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()

    return None


class OBJECT_OT_gemini_run(bpy.types.Operator):
    bl_idname = "object.gemini_run"
    bl_label = "生成腳本"
    bl_description = "將提示詞發送給 Gemini 生成 Python 腳本 (支援快取、冷卻防護與非同步背景執行)"

    def execute(self, context):
        global _request_state
        settings = context.scene.gemini_settings
        prompt_clean = settings.prompt.strip()

        if not prompt_clean:
            self.report({'ERROR'}, "請輸入提示詞！")
            settings.status = "請輸入提示詞"
            return {'CANCELLED'}

        api_key, model_id, max_tokens, temperature = get_active_config(context)
        if not api_key:
            self.report({'ERROR'}, "請先輸入 Gemini API Key！")
            settings.status = "錯誤：缺少 API Key"
            return {'CANCELLED'}

        # 1. 檢查是否正在進行非同步請求
        if _request_state["is_running"]:
            self.report({'WARNING'}, "AI 正在生成中，請稍候...")
            return {'CANCELLED'}

        # 2. 防連點冷卻防護
        now = time.time()
        elapsed = now - _request_state["last_request_time"]
        if elapsed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - elapsed) + 1
            settings.status = f"冷卻保護中 (請等待 {remaining} 秒)"
            self.report({'WARNING'}, f"呼叫頻率過高！請等待 {remaining} 秒防爆冷卻結束")
            return {'CANCELLED'}

        # 3. 本地快取檢查 (0 Token 消耗)
        if prompt_clean in _prompt_cache:
            cached_code = _prompt_cache[prompt_clean]
            clean_code = extract_python_code(cached_code)
            write_code_to_text_editor(clean_code)
            if settings.auto_execute:
                success, msg, _ = execute_generated_code(clean_code, self.report)
                if success:
                    settings.status = f"已從本地快取載入並執行: {msg}"
                else:
                    settings.status = f"快取代碼執行出錯: {msg}"
            else:
                settings.status = "已從本地快取載入至文字編輯器 (0 Token 消耗)"
                self.report({'INFO'}, "已從本地快取取得腳本，未消耗任何 API 配額！")
            return {'FINISHED'}

        # 4. 啟動背景非同步線程發送請求
        _request_state["is_running"] = True
        _request_state["last_request_time"] = now
        _request_state["result"] = None

        settings.status = f"AI 思考中 ({model_id})..."

        worker_thread = threading.Thread(
            target=_async_gemini_worker,
            args=(api_key, model_id, max_tokens, temperature, prompt_clean),
            daemon=True
        )
        worker_thread.start()

        bpy.app.timers.register(_gemini_poll_timer, first_interval=0.1)

        self.report({'INFO'}, f"已向 Gemini ({model_id}) 發送請求，背景生成中...")
        return {'FINISHED'}


# 9. 現代化 UI 面板
class VIEW3D_PT_gemini_copilot(bpy.types.Panel):
    bl_label = "Gemini Copilot v2.5"
    bl_idname = "VIEW3D_PT_gemini_copilot"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Gemini Copilot'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.gemini_settings
        prefs = context.preferences.addons.get(addon_name)

        # 區塊 1: 連線與配置
        box = layout.box()
        row = box.row(align=True)
        row.label(text="API 配置 (Gemini 3.x)", icon='SETTINGS')
        row.operator("object.gemini_test_connection", text="測試連線", icon='CHECKMARK')

        if prefs:
            p = prefs.preferences
            box.prop(p, "api_key")
            box.prop(p, "model")
            if p.model == 'CUSTOM':
                box.prop(p, "custom_model")
        else:
            box.prop(settings, "api_key_fallback")
            box.prop(settings, "model_fallback")
            if settings.model_fallback == 'CUSTOM':
                box.prop(settings, "custom_model_fallback")

        # 區塊 2: 常用離線範本庫 (0 Token 專區)
        box_preset = layout.box()
        box_preset.label(text="常用離線範本 (0 Token 免連線)", icon='FILE_CACHE')
        row = box_preset.row(align=True)
        row.prop(settings, "preset_selection", text="")
        row.operator("object.gemini_load_preset", text="載入範本", icon='FORWARD')

        # 區塊 3: AI 提示詞生成
        layout.separator()
        layout.label(text="AI 提示詞生成 (純淨物件建模):", icon='LIGHT')
        layout.prop(settings, "prompt", text="")

        row_opt = layout.row(align=True)
        row_opt.prop(settings, "auto_execute", toggle=True, icon='PLAY')

        cached_count = len(_prompt_cache)
        cache_text = f"快取: {cached_count} 筆" if cached_count > 0 else "快取: 空"
        row_opt.label(text=cache_text, icon='FILE_REFRESH')

        # 核心執行按鈕
        col = layout.column(align=True)
        if _request_state["is_running"]:
            col.label(text="AI 生成中，請稍候...", icon='TIME')
        else:
            btn_text = "生成並執行腳本" if settings.auto_execute else "僅生成至文字編輯器"
            col.operator("object.gemini_run", text=btn_text, icon='FORCE_VORTEX')

        # 視圖對焦與輔助工具
        row_sub = layout.row(align=True)
        row_sub.operator("object.gemini_focus_object", text="視圖對焦物件", icon='VIEW_PAN')
        row_sub.operator("object.gemini_run_editor_code", text="執行編輯器腳本", icon='TEXT')
        row_sub.operator("object.gemini_clear_cache", text="清除快取", icon='TRASH')

        # 區塊 4: 狀態反饋框
        box_status = layout.box()
        status_row = box_status.row()
        if "錯誤" in settings.status or "失敗" in settings.status or "出錯" in settings.status or "429" in settings.status or "503" in settings.status:
            status_row.alert = True
            status_row.label(text=f"狀態: {settings.status}", icon='ERROR')
        elif "成功" in settings.status or "已建立" in settings.status:
            status_row.label(text=f"狀態: {settings.status}", icon='CHECKMARK')
        elif "思考中" in settings.status or "測試中" in settings.status:
            status_row.label(text=f"狀態: {settings.status}", icon='TIME')
        else:
            status_row.label(text=f"狀態: {settings.status}", icon='INFO')


# 10. 註冊與反註冊
classes = (
    GeminiCopilotPreferences,
    GeminiCopilotSettings,
    OBJECT_OT_gemini_test_connection,
    OBJECT_OT_gemini_load_preset,
    OBJECT_OT_gemini_run_editor_code,
    OBJECT_OT_gemini_focus_object,
    OBJECT_OT_gemini_clear_cache,
    OBJECT_OT_gemini_run,
    VIEW3D_PT_gemini_copilot,
)

def register():
    install_compatibility_polyfills()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gemini_settings = bpy.props.PointerProperty(type=GeminiCopilotSettings)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "gemini_settings"):
        del bpy.types.Scene.gemini_settings

if __name__ == "__main__":
    register()