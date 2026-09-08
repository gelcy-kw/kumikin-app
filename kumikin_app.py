import streamlit as st
import pandas as pd
import re
import traceback
from ortools.sat.python import cp_model

# 1. ページ設定＆自動翻訳エラー対策
st.set_page_config(page_title="勤務変更補助システム", layout="centered")
st.markdown('<meta name="google" content="notranslate">', unsafe_allow_html=True)

def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 アクセス制限")
        pwd = st.text_input("パスコードを入力してください", type="password")
        if st.button("ログイン"):
            if pwd == "1026":
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("パスコードが正しくありません")
        return False
    return True

def load_csv_safely(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file, encoding='utf-8')
    except (UnicodeDecodeError, pd.errors.ParserError):
        uploaded_file.seek(0)
        df = pd.read_csv(uploaded_file, encoding='cp932')
    df.columns = df.columns.str.strip()
    return df

def clean_str(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    return s[:-2].upper() if s.endswith('.0') else s.upper()

def parse_trade_allowed(val):
    if pd.isna(val):
        return 'Y'
    s = str(val).strip().upper()
    if s in ['N', 'NO', '0', 'FALSE', 'NG', '固定', '不可']:
        return 'N'
    return 'Y'

def parse_int_safely(val):
    if pd.isna(val):
        return 0
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return 0

def normalize_area_dynamic(val):
    s = clean_str(val)
    return s if s else 'ANY'

def is_off_or_vacation(task_code):
    if not task_code:
        return True
    OFF_KEYWORDS = ['週休', '休暇', '公休', '有休', '特休', '代休', 'OFF', '明']
    return any(kw in task_code for kw in OFF_KEYWORDS)

OFF_KEYWORDS = ['週休', '休暇', '公休', '有休', '特休', '代休', 'OFF', '明']

def render_custom_html_table(df, id_col_name, day_lock_flags, changed_cells, overflow_cells, max_height="500px"):
    html = f"""
    <div style="overflow-x: auto; max-height: {max_height}; border: 1px solid #dee2e6; border-radius: 6px; margin-bottom: 15px;">
    <table style="border-collapse: collapse; width: 100%; font-size: 12px; text-align: center; font-family: sans-serif;">
        <thead><tr style="background-color: #f8f9fa; position: sticky; top: 0; z-index: 10; box-shadow: 0 1px 2px rgba(0,0,0,0.1);">
    """
    for col in df.columns:
        html += f'<th style="border: 1px solid #dee2e6; padding: 8px 10px; white-space: nowrap; color: #495057; font-weight: 600;">{col}</th>'
    html += "</tr></thead><tbody>"

    for idx, row in df.iterrows():
        p_id = str(row[id_col_name])
        html += "<tr>"
        for col in df.columns:
            cell_val = str(row[col])
            str_col = str(col)
            is_locked = day_lock_flags.get(str_col, False)
            is_changed = (p_id, str_col) in changed_cells
            is_overflow = (p_id, str_col) in overflow_cells
            is_off = any(kw in cell_val for kw in OFF_KEYWORDS)

            bg = "#ffffff"
            color = "#212529"
            weight = "normal"

            if is_off:
                bg = "#f8d7da" if is_locked else "#ffffff"
                color = "#d9534f"
                weight = "bold"
            elif is_locked:
                bg = "#f8d7da"
                color = "#721c24"
            elif is_overflow:
                bg = "#fff3cd"
                color = "#856404"
                weight = "bold"
            elif is_changed:
                bg = "#d4edda"
                color = "#155724"
                weight = "bold"

            html += f'<td style="background-color: {bg}; color: {color}; font-weight: {weight}; border: 1px solid #dee2e6; padding: 6px 8px; white-space: nowrap;">{cell_val}</td>'
        html += "</tr>"
    html += "</tbody></table></div>"
    return html

def generate_full_html_document(df, id_col_name, day_lock_flags, changed_cells, overflow_cells):
    table_body = render_custom_html_table(df, id_col_name, day_lock_flags, changed_cells, overflow_cells, max_height="none")
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>最適化シフト結果</title>
    <style>
        body {{ font-family: 'Helvetica Neue', Arial, sans-serif; padding: 20px; background-color: #fafafa; }}
        h2 {{ color: #333; margin-bottom: 10px; }}
        .legend {{ font-size: 12px; margin-bottom: 15px; color: #666; }}
        .legend span {{ display: inline-block; padding: 3px 8px; margin-right: 10px; border-radius: 3px; font-weight: bold; }}
        .changed {{ background-color: #d4edda; color: #155724; }}
        .overflow {{ background-color: #fff3cd; color: #856404; }}
        .locked {{ background-color: #f8f9fa; color: #721c24; }}
        .off {{ color: #d9534f; }}
    </style>
</head>
<body>
    <h2>📊 最適化シフト結果</h2>
    <div class="legend">
        凡例: 
        <span class="changed">トレード変更</span>
        <span class="overflow">エリア不一致（溢れ）</span>
        <span class="locked">LOCK指定日</span>
        <span class="off">休日セル</span>
    </div>
    {table_body}
</body>
</html>
"""

@st.dialog("📊 最適化結果（大画面プレビュー）", width="large")
def show_large_preview(df, id_col_name, day_lock_flags, changed_cells, overflow_cells):
    st.markdown(render_custom_html_table(df, id_col_name, day_lock_flags, changed_cells, overflow_cells, max_height="75vh"), unsafe_allow_html=True)

if check_password():
    st.title("勤務変更補助システム")
    st.caption("自動シフトトレード・エリア最適化ソルバー (1対1トレード厳格化版)")

    st.subheader("1. データファイルのアップロード")
    file_members = st.file_uploader("メンバーマスター (Member_Master.csv)", type=["csv"], key="u_mem")
    file_tasks = st.file_uploader("仕業マスター (Task_Master.csv)", type=["csv"], key="u_task")
    file_initial = st.file_uploader("初期勤務表 (Initial_Schedule.csv)", type=["csv"], key="u_init")

    def run_optimization(df_members, df_tasks, df_initial_raw):
        debug_logs = []
        def log(msg):
            debug_logs.append(msg)

        try:
            log("--- 最適化処理を開始します ---")
            id_col_name = df_initial_raw.columns[0]
            name_col_name = df_initial_raw.columns[1]

            all_cols = list(df_initial_raw.columns)
            meta_cols = [id_col_name, name_col_name]
            for col in all_cols:
                c_upper = col.upper().strip()
                if c_upper in ['OF_M1', 'OF_M2']:
                    meta_cols.append(col)

            dates = [clean_str(c) for c in all_cols if c not in meta_cols]

            lock_row = df_initial_raw[df_initial_raw[id_col_name].apply(clean_str) == 'LOCK']
            day_lock_flags = {}
            for d in dates:
                if not lock_row.empty and d in lock_row.columns:
                    val = clean_str(lock_row.iloc[0][d])
                    day_lock_flags[d] = val in ['LOCK', '1', 'YES', 'TRUE', '固定']
                else:
                    day_lock_flags[d] = False

            daytype_row = df_initial_raw[df_initial_raw[id_col_name].apply(clean_str) == 'DAYTYPE']

            df_members['MemberID'] = df_members['MemberID'].apply(clean_str)
            member_base_area = {}
            member_role = {}
            member_gender = {}
            
            for _, row in df_members.iterrows():
                m_id = clean_str(row['MemberID'])
                member_base_area[m_id] = normalize_area_dynamic(row.get('BaseArea', ''))
                member_role[m_id] = clean_str(row.get('Role', ''))
                member_gender[m_id] = clean_str(row.get('Gender', ''))

            members = list(member_base_area.keys())

            task_area_map = {}
            task_female_allowed_map = {}
            task_trade_allowed_map = {}
            pair_rules = {}

            if 'TaskID' in df_tasks.columns:
                for _, row in df_tasks.iterrows():
                    t_id = clean_str(row['TaskID'])
                    t_area = normalize_area_dynamic(row.get('TargetArea', ''))
                    f_allowed = clean_str(row.get('FemaleAllowed', 'Y'))
                    trade_allowed = parse_trade_allowed(row.get('TradeAllowed')) if 'TradeAllowed' in df_tasks.columns else ('N' if (t_id and not t_id[0].isdigit()) else 'Y')
                    pair_id = clean_str(row.get('PairTaskID', ''))

                    task_area_map[t_id] = t_area
                    task_female_allowed_map[t_id] = f_allowed
                    task_trade_allowed_map[t_id] = trade_allowed

                    m_match = re.match(r'^(\d+)([MC])$', t_id)
                    if m_match and pair_id and pair_id.isdigit():
                        prev_plain_id = f"{pair_id}{m_match.group(2)}"
                        pair_rules[prev_plain_id] = t_id

                    m_match_long = re.match(r'([MC])_(\d+)_[WH]', t_id) or re.match(r'([MC])_(\d+)', t_id)
                    if m_match_long:
                        role_char, num_str = m_match_long.group(1), m_match_long.group(2)
                        plain_id = f"{num_str}{role_char}"
                        task_area_map[plain_id] = t_area
                        task_female_allowed_map[plain_id] = f_allowed
                        task_trade_allowed_map[plain_id] = trade_allowed
                        if pair_id and pair_id.isdigit():
                            pair_rules[f"{pair_id}{role_char}"] = plain_id

            for _, row in df_tasks.iterrows():
                t_id = clean_str(row['TaskID'])
                pair_id = clean_str(row.get('PairTaskID', ''))
                if pair_id and not pair_id.isdigit():
                    pair_rules[pair_id] = t_id

            def get_task_area(task_code):
                return 'ANY' if is_off_or_vacation(task_code) else task_area_map.get(task_code, 'ANY')

            def is_female_allowed(task_code):
                return task_female_allowed_map.get(task_code, 'Y') != 'N'

            def is_trade_allowed(task_code):
                if is_off_or_vacation(task_code):
                    return False
                if task_code not in task_trade_allowed_map:
                    return not (task_code and not task_code[0].isdigit())
                return task_trade_allowed_map.get(task_code, 'Y') == 'Y'

            ignored_rows = ['DAYTYPE', 'LOCK']
            df_sched = df_initial_raw[~df_initial_raw[id_col_name].apply(clean_str).isin(ignored_rows)].copy()
            df_sched[id_col_name] = df_sched[id_col_name].apply(clean_str)

            member_names = {}
            member_past_overflow = {}
            col_m1 = next((c for c in df_sched.columns if c.upper().strip() == 'OF_M1'), None)
            col_m2 = next((c for c in df_sched.columns if c.upper().strip() == 'OF_M2'), None)

            for _, row in df_sched.iterrows():
                m_id = clean_str(row[id_col_name])
                member_names[m_id] = str(row[name_col_name]).strip() if pd.notna(row[name_col_name]) else m_id
                member_past_overflow[m_id] = parse_int_safely(row[col_m1]) + parse_int_safely(row[col_m2])

            df_initial_indexed = df_sched.set_index(id_col_name)
            existing_members = [m for m in members if m in df_initial_indexed.index]

            if not existing_members:
                return df_initial_raw, False, "メンバーIDが一致しませんでした", [], [], [], set(), set(), "", {}, debug_logs

            initial_assignment = {}
            all_tasks_set = set()
            tasks_by_day = {}

            for d in dates:
                tasks_by_day[d] = set()
                for p in existing_members:
                    val = str(df_initial_indexed.loc[p, d]).strip() if pd.notna(df_initial_indexed.loc[p, d]) else '公休'
                    val = clean_str(val) if val else '公休'
                    initial_assignment[(p, d)] = val
                    all_tasks_set.add(val)
                    tasks_by_day[d].add(val)

            all_tasks = list(all_tasks_set)

            # --- CP-SAT モデル構築 ---
            model = cp_model.CpModel()
            x = {}
            for p in existing_members:
                for d in dates:
                    for t in all_tasks:
                        x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

            for d in dates:
                for p in existing_members:
                    model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

            # トレード不可・LOCK日制約
            for p in existing_members:
                for d in dates:
                    orig_t = initial_assignment.get((p, d), '公休')
                    if not is_trade_allowed(orig_t) or day_lock_flags.get(d, False):
                        model.Add(x[p, d, orig_t] == 1)

            # Role / Gender 制約
            for p in existing_members:
                p_role = member_role.get(p, '')
                p_gender = member_gender.get(p, '')
                for d in dates:
                    if day_lock_flags.get(d, False):
                        continue
                    for t in all_tasks:
                        if not is_trade_allowed(t):
                            continue
                        if (p_role == 'M' and t.endswith('C')) or (p_role == 'C' and t.endswith('M')):
                            model.Add(x[p, d, t] == 0)
                        if p_gender == 'F' and not is_female_allowed(t):
                            model.Add(x[p, d, t] == 0)

            # 【重要】完全1対1（ペア）トレード制約の構築
            # 日ごとに、トレードを行う場合は必ず「2人ペア」で互いに仕業を交換しなければならない
            for d in dates:
                if day_lock_flags.get(d, False):
                    continue
                
                # トレード対象メンバーのペア変数作成
                pair_swaps = {}
                for i in range(len(existing_members)):
                    p1 = existing_members[i]
                    t1_orig = initial_assignment.get((p1, d), '公休')
                    if not is_trade_allowed(t1_orig):
                        continue
                    
                    for j in range(i + 1, len(existing_members)):
                        p2 = existing_members[j]
                        t2_orig = initial_assignment.get((p2, d), '公休')
                        if not is_trade_allowed(t2_orig) or t1_orig == t2_orig:
                            continue

                        swap_var = model.NewBoolVar(f'swap_{p1}_{p2}_{d}')
                        pair_swaps[(p1, p2)] = swap_var

                        # swap_var = 1 のとき、p1はt2_origを、p2はt1_origを担当
                        model.Add(x[p1, d, t2_orig] == 1).OnlyEnforceIf(swap_var)
                        model.Add(x[p2, d, t1_orig] == 1).OnlyEnforceIf(swap_var)

                # 各メンバーは「元の仕業を維持する」か「誰か1人と入れ替える（1対1）」のどちらか1つのみ
                for p in existing_members:
                    t_orig = initial_assignment.get((p, d), '公休')
                    if not is_trade_allowed(t_orig):
                        continue
                    
                    p_swaps = [swap_var for (p1, p2), swap_var in pair_swaps.items() if p1 == p or p2 == p]
                    if p_swaps:
                        # 元の仕業を維持するか、トレードペアの1つに参加する
                        model.Add(x[p, d, t_orig] + sum(p_swaps) == 1)

            # 連番仕業（2日連続ペア制約）の維持
            for d_idx in range(len(dates) - 1):
                d_curr, d_next = dates[d_idx], dates[d_idx + 1]
                for work_curr, work_next_required in pair_rules.items():
                    if work_curr in tasks_by_day[d_curr] and work_next_required in tasks_by_day[d_next]:
                        for p in existing_members:
                            model.Add(x[p, d_curr, work_curr] == x[p, d_next, work_next_required])

            # ベースエリア外トレードの制限
            for p in existing_members:
                p_base_area = member_base_area.get(p, 'ANY')
                if p_base_area != 'ANY':
                    for d in dates:
                        if day_lock_flags.get(d, False):
                            continue
                        orig_t = initial_assignment.get((p, d), '公休')
                        for t in all_tasks:
                            if not is_trade_allowed(t) or t == orig_t:
                                continue
                            t_area = get_task_area(t)
                            if t_area != 'ANY' and t_area != p_base_area:
                                model.Add(x[p, d, t] == 0)

            # --- 目的関数＆エリア溢れの均等化（平準化） ---
            objective_terms = []

            # 3連番ルールの一括トレード（優先度の高い1対1連番交換）
            triple_rules = []
            for t1, t2 in pair_rules.items():
                if t2 in pair_rules:
                    t3 = pair_rules[t2]
                    if t1 in all_tasks and t2 in all_tasks and t3 in all_tasks:
                        triple_rules.append((t1, t2, t3))

            triple_trade_vars = []
            if len(dates) >= 3 and triple_rules:
                for d_idx in range(len(dates) - 2):
                    d1, d2, d3 = dates[d_idx], dates[d_idx + 1], dates[d_idx + 2]
                    if day_lock_flags.get(d1) or day_lock_flags.get(d2) or day_lock_flags.get(d3):
                        continue

                    p_triples = {}
                    for p in existing_members:
                        p_t1, p_t2, p_t3 = initial_assignment.get((p, d1)), initial_assignment.get((p, d2)), initial_assignment.get((p, d3))
                        if (p_t1, p_t2, p_t3) in triple_rules and is_trade_allowed(p_t1) and is_trade_allowed(p_t2) and is_trade_allowed(p_t3):
                            p_triples[p] = (p_t1, p_t2, p_t3)

                    p_list = list(p_triples.keys())
                    for p1_i in range(len(p_list)):
                        for p2_i in range(p1_i + 1, len(p_list)):
                            p1, p2 = p_list[p1_i], p_list[p2_i]
                            (p1_t1, p1_t2, p1_t3) = p_triples[p1]
                            (p2_t1, p2_t2, p2_t3) = p_triples[p2]

                            if (p1_t1, p1_t2, p1_t3) != (p2_t1, p2_t2, p2_t3):
                                triple_swap_var = model.NewBoolVar(f'tr_sw_{p1}_{p2}_{d1}')
                                model.Add(x[p1, d1, p2_t1] == 1).OnlyEnforceIf(triple_swap_var)
                                model.Add(x[p1, d2, p2_t2] == 1).OnlyEnforceIf(triple_swap_var)
                                model.Add(x[p1, d3, p2_t3] == 1).OnlyEnforceIf(triple_swap_var)
                                model.Add(x[p2, d1, p1_t1] == 1).OnlyEnforceIf(triple_swap_var)
                                model.Add(x[p2, d2, p1_t2] == 1).OnlyEnforceIf(triple_swap_var)
                                model.Add(x[p2, d3, p1_t3] == 1).OnlyEnforceIf(triple_swap_var)

                                objective_terms.append(triple_swap_var * -100000)
                                triple_trade_vars.append((p1, p2, d1, d2, d3, (p1_t1, p1_t2, p1_t3), (p2_t1, p2_t2, p2_t3), triple_swap_var))

            # エリア溢れ（OverFlow）計算と平準化
            member_total_of_vars = []
            for p in existing_members:
                p_base_area = member_base_area.get(p, 'ANY')
                p_of_terms = []
                if p_base_area != 'ANY':
                    for d in dates:
                        if day_lock_flags.get(d, False):
                            continue
                        for t in all_tasks:
                            if not is_trade_allowed(t):
                                continue
                            t_area = get_task_area(t)
                            if t_area != 'ANY' and t_area != p_base_area:
                                p_of_terms.append(x[p, d, t])
                
                curr_of_var = model.NewIntVar(0, len(dates), f'of_{p}')
                model.Add(curr_of_var == sum(p_of_terms))

                past_of = member_past_overflow.get(p, 0)
                total_of_var = model.NewIntVar(0, len(dates) + 50, f'tot_of_{p}')
                model.Add(total_of_var == curr_of_var + past_of)
                member_total_of_vars.append(total_of_var)

                # 個別のペナルティ
                objective_terms.append(curr_of_var * 1000)

            # 【溢れ平準化】全員の合計溢れ（過去含む）の最大値を抑え込んで均等化
            max_of_var = model.NewIntVar(0, 100, 'max_overflow')
            for tot_v in member_total_of_vars:
                model.Add(tot_v <= max_of_var)
            objective_terms.append(max_of_var * 10000)  # 最大溢れ数を強力に抑制して平準化

            # 変更件数最小化ペナルティ
            for p in existing_members:
                for d in dates:
                    if day_lock_flags.get(d, False):
                        continue
                    orig_t = initial_assignment.get((p, d), '公休')
                    for t in all_tasks:
                        if is_trade_allowed(t) and t != orig_t:
                            objective_terms.append(x[p, d, t] * 1)

            model.Minimize(sum(objective_terms))

            log("ソルバーを実行中...")
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = 30.0
            solver.parameters.num_search_workers = 2
            
            status = solver.Solve(model)
            status_name = solver.StatusName(status)
            log(f"ソルバー実行完了 Status: {status_name}")

            change_logs = []
            triple_applied_logs = []
            changed_cells = set()
            overflow_cells = set()

            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                final_schedule = {}
                for d in dates:
                    for p in existing_members:
                        for t in all_tasks:
                            if solver.Value(x[p, d, t]) == 1:
                                final_schedule[(p, d)] = t
                                orig_t = initial_assignment.get((p, d), '公休')
                                if t != orig_t:
                                    p_name = member_names.get(p, p)
                                    change_logs.append(f"【{d}】{p_name}さん({p}) : {orig_t} ➔ {t}")
                                    changed_cells.add((p, d))
                                break

                for p1, p2, d1, d2, d3, (p1_t1, p1_t2, p1_t3), (p2_t1, p2_t2, p2_t3), svar in triple_trade_vars:
                    if solver.Value(svar) == 1:
                        name1 = member_names.get(p1, p1)
                        name2 = member_names.get(p2, p2)
                        triple_applied_logs.append(
                            f"【3連番一括1対1トレード成立】{d1}〜{d3} : {name1}さん ({p1_t1}->{p1_t2}->{p1_t3}) 🔁 {name2}さん ({p2_t1}->{p2_t2}->{p2_t3})"
                        )

                result_rows = []
                if not daytype_row.empty:
                    r_dict = daytype_row.iloc[0].to_dict()
                    r_dict['OverFlow'], r_dict['3M_Total_OF'] = '', ''
                    result_rows.append(r_dict)
                if not lock_row.empty:
                    r_dict = lock_row.iloc[0].to_dict()
                    r_dict['OverFlow'], r_dict['3M_Total_OF'] = '', ''
                    result_rows.append(r_dict)

                for p in existing_members:
                    p_base_area = member_base_area.get(p, 'ANY')
                    overflow_count = 0
                    row_src = df_initial_indexed.loc[p]
                    row = {id_col_name: p, name_col_name: member_names.get(p, '')}

                    if col_m1:
                        row[col_m1] = parse_int_safely(row_src.get(col_m1, 0))
                    if col_m2:
                        row[col_m2] = parse_int_safely(row_src.get(col_m2, 0))

                    for d in dates:
                        task_assigned = final_schedule.get((p, d), initial_assignment.get((p, d), '公休'))
                        row[d] = task_assigned
                        
                        if not day_lock_flags.get(d, False) and is_trade_allowed(task_assigned):
                            t_area = get_task_area(task_assigned)
                            if p_base_area != 'ANY' and t_area != 'ANY' and p_base_area != t_area:
                                overflow_count += 1
                                overflow_cells.add((p, d))

                    row['OverFlow'] = int(overflow_count)
                    past_of = member_past_overflow.get(p, 0)
                    row['3M_Total_OF'] = int(past_of + overflow_count)
                    result_rows.append(row)

                df_result = pd.DataFrame(result_rows)

                for m_col in [col_m1, col_m2]:
                    if m_col and m_col in df_result.columns:
                        df_result[m_col] = df_result[m_col].apply(
                            lambda v: int(float(v)) if pd.notna(v) and str(v).strip() != '' and str(v).replace('.','',1).isdigit() else ''
                        )

                return df_result, True, "OK", change_logs, [], triple_applied_logs, changed_cells, overflow_cells, id_col_name, day_lock_flags, debug_logs
            else:
                return df_initial_raw, False, f"Solver Status: {status_name}", [], [], [], set(), set(), "", {}, debug_logs

        except Exception as e:
            err_msg = traceback.format_exc()
            log(err_msg)
            return df_initial_raw, False, f"Exception: {str(e)}", [], [], [], set(), set(), "", {}, debug_logs

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行", key="btn_run"):
        if file_members and file_tasks and file_initial:
            with st.spinner("計算中...（数秒で完了します）"):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg, change_logs, _, triple_logs, changed_cells, overflow_cells, id_col, day_lock_flags, debug_logs = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("最適化計算が完了しました！")

                    if triple_logs:
                        st.subheader("🔥 優先適用された【3連番一括1対1トレード】")
                        for tlog in triple_logs:
                            st.success(tlog)

                    if change_logs:
                        st.subheader("📋 変更（トレード）された勤務一覧")
                        for clog in change_logs:
                            st.write(clog)
                    else:
                        st.info("ℹ️ 初期シフトから変更の必要はありませんでした。")

                    st.subheader("📊 最適化結果プレビュー")
                    
                    if st.button("🔍 プレビューを大画面（全画面風）で確認する", key="btn_modal"):
                        show_large_preview(result_df, id_col, day_lock_flags, changed_cells, overflow_cells)

                    st.markdown(render_custom_html_table(result_df, id_col, day_lock_flags, changed_cells, overflow_cells, max_height="500px"), unsafe_allow_html=True)

                    st.subheader("📥 結果ダウンロード")
                    col1, col2 = st.columns(2)

                    with col1:
                        csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                        st.download_button(
                            label="📥 CSVデータをダウンロード",
                            data=csv_data,
                            file_name="Optimized_Schedule.csv",
                            mime="text/csv",
                            use_container_width=True,
                            key="dl_csv"
                        )

                    with col2:
                        full_html_str = generate_full_html_document(result_df, id_col, day_lock_flags, changed_cells, overflow_cells)
                        st.download_button(
                            label="🎨 色付きHTML（印刷/PDF化用）をダウンロード",
                            data=full_html_str.encode('utf-8-sig'),
                            file_name="Optimized_Schedule_Colored.html",
                            mime="text/html",
                            use_container_width=True,
                            key="dl_html"
                        )

                else:
                    st.error(f"解が見つからなかったか、タイムアウトしました。（詳細: {log_msg}）")

                with st.expander("🐛 実行・デバッグログ", expanded=not success):
                    st.code("\n".join(debug_logs), language="text")
        else:
            st.error("エラー: 3つのファイルをすべてアップロードしてください。")