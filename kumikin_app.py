import streamlit as st
import pandas as pd
import re
import traceback
import io
from ortools.sat.python import cp_model

st.set_page_config(page_title="勤務変更補助システム", layout="centered")

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

if check_password():
    st.title("勤務変更補助システム")
    st.caption("自動シフトトレード・エリア最適化ソルバー")

    st.subheader("1. データファイルのアップロード")
    file_members = st.file_uploader("メンバーマスター (Member_Master.csv)", type=["csv"])
    file_tasks = st.file_uploader("仕業マスター (Task_Master.csv)", type=["csv"])
    file_initial = st.file_uploader("初期勤務表 (Initial_Schedule.csv)", type=["csv"])

    def run_optimization(df_members, df_tasks, df_initial_raw):
        debug_logs = []
        def log(msg):
            debug_logs.append(msg)

        try:
            log("--- 最適化処理を開始します ---")
            id_col_name = df_initial_raw.columns[0]
            name_col_name = df_initial_raw.columns[1]
            log(f"ID列名: '{id_col_name}', 名前列名: '{name_col_name}'")

            all_cols = list(df_initial_raw.columns)
            meta_cols = [id_col_name, name_col_name]
            
            for col in all_cols:
                c_upper = col.upper().strip()
                if c_upper in ['OF_M1', 'OF_M2']:
                    meta_cols.append(col)

            dates = [clean_str(c) for c in all_cols if c not in meta_cols]
            log(f"検出された対象日付（{len(dates)}件）: {dates}")

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
                area = normalize_area_dynamic(row.get('BaseArea', ''))
                role = clean_str(row.get('Role', ''))
                gender = clean_str(row.get('Gender', ''))
                
                member_base_area[m_id] = area
                member_role[m_id] = role
                member_gender[m_id] = gender

            members = list(member_base_area.keys())
            log(f"メンバーマスター件数: {len(members)}名")

            task_area_map = {}
            task_female_allowed_map = {}
            task_trade_allowed_map = {}
            task_start_type = {}
            task_end_type = {}
            task_load_map = {}
            pair_rules = {}

            if 'TaskID' in df_tasks.columns:
                load_col = df_tasks.columns[2] if len(df_tasks.columns) > 2 else 'Load'
                for _, row in df_tasks.iterrows():
                    t_id = clean_str(row['TaskID'])
                    t_area = normalize_area_dynamic(row.get('TargetArea', ''))
                    f_allowed = clean_str(row.get('FemaleAllowed', 'Y'))
                    
                    t_load = parse_int_safely(row.get(load_col, 0)) if load_col in row else parse_int_safely(row.get('Load', 0))

                    if 'TradeAllowed' in df_tasks.columns:
                        trade_allowed = parse_trade_allowed(row.get('TradeAllowed'))
                    else:
                        trade_allowed = 'N' if (t_id and not t_id[0].isdigit()) else 'Y'

                    pair_id = clean_str(row.get('PairTaskID', ''))

                    s_type = clean_str(row.get('StartType', ''))
                    e_type = clean_str(row.get('EndType', ''))

                    task_area_map[t_id] = t_area
                    task_female_allowed_map[t_id] = f_allowed
                    task_trade_allowed_map[t_id] = trade_allowed
                    task_start_type[t_id] = s_type
                    task_end_type[t_id] = e_type
                    task_load_map[t_id] = t_load

                    m_match = re.match(r'^(\d+)([MC])$', t_id)
                    if m_match:
                        role_char = m_match.group(2)
                        if pair_id and pair_id.isdigit():
                            prev_plain_id = f"{pair_id}{role_char}"
                            pair_rules[prev_plain_id] = t_id

                    m_match_long = re.match(r'([MC])_(\d+)_[WH]', t_id)
                    if not m_match_long:
                        m_match_long = re.match(r'([MC])_(\d+)', t_id)
                    if m_match_long:
                        role_char = m_match_long.group(1)
                        num_str = m_match_long.group(2)
                        plain_id = f"{num_str}{role_char}"
                        
                        task_area_map[plain_id] = t_area
                        task_female_allowed_map[plain_id] = f_allowed
                        task_trade_allowed_map[plain_id] = trade_allowed
                        task_start_type[plain_id] = s_type
                        task_end_type[plain_id] = e_type
                        task_load_map[plain_id] = t_load

                        if pair_id and pair_id.isdigit():
                            prev_plain_id = f"{pair_id}{role_char}"
                            pair_rules[prev_plain_id] = plain_id

            for _, row in df_tasks.iterrows():
                t_id = clean_str(row['TaskID'])
                pair_id = clean_str(row.get('PairTaskID', ''))
                if pair_id and not pair_id.isdigit():
                    pair_rules[pair_id] = t_id

            log(f"構築されたペア制約数: {len(pair_rules)}件")

            first_day_pair_tasks = set(pair_rules.keys())
            second_day_pair_tasks = set(pair_rules.values())

            def get_task_area(task_code):
                if is_off_or_vacation(task_code):
                    return 'ANY'
                return task_area_map.get(task_code, 'ANY')

            def is_female_allowed(task_code):
                return task_female_allowed_map.get(task_code, 'Y') != 'N'

            def is_trade_allowed(task_code):
                if is_off_or_vacation(task_code):
                    return False
                if task_code not in task_trade_allowed_map:
                    if task_code and not task_code[0].isdigit():
                        return False
                    return True
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
                m_name = str(row[name_col_name]).strip() if pd.notna(row[name_col_name]) else m_id
                member_names[m_id] = m_name

                of1 = parse_int_safely(row[col_m1]) if col_m1 else 0
                of2 = parse_int_safely(row[col_m2]) if col_m2 else 0
                member_past_overflow[m_id] = of1 + of2

            df_initial_indexed = df_sched.set_index(id_col_name)
            
            existing_members = [m for m in members if m in df_initial_indexed.index]
            log(f"初期勤務表に存在する有効メンバー数: {len(existing_members)}名")

            if len(existing_members) == 0:
                log("エラー: 初期勤務表のIDとメンバーマスターのIDが一致しません。")
                return df_initial_raw, False, "メンバーIDが一致しませんでした", [], [], [], set(), set(), id_col_name, {}, debug_logs

            initial_assignment = {}
            all_tasks_set = set()
            tasks_by_day = {}

            for d in dates:
                tasks_by_day[d] = set()
                for p in existing_members:
                    val = str(df_initial_indexed.loc[p, d]).strip() if pd.notna(df_initial_indexed.loc[p, d]) else '公休'
                    if not val:
                        val = '公休'
                    val = clean_str(val)
                    initial_assignment[(p, d)] = val
                    all_tasks_set.add(val)
                    tasks_by_day[d].add(val)

            all_tasks = list(all_tasks_set)
            log(f"検出されたユニーク仕業数: {len(all_tasks)}件")

            first_date = dates[0] if dates else None
            last_date = dates[-1] if dates else None

            def is_boundary_pair_task(p, d):
                orig_t = initial_assignment.get((p, d), '公休')
                if d == last_date and orig_t in first_day_pair_tasks:
                    return True
                if d == first_date and orig_t in second_day_pair_tasks:
                    return True
                return False

            model = cp_model.CpModel()
            x = {}
            for p in existing_members:
                for d in dates:
                    for t in all_tasks:
                        x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

            for d in dates:
                for p in existing_members:
                    model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

            for p in existing_members:
                for d in dates:
                    orig_t = initial_assignment.get((p, d), '公休')
                    
                    is_last_day_pair_first = (d == last_date and orig_t in first_day_pair_tasks)
                    is_first_day_pair_second = (d == first_date and orig_t in second_day_pair_tasks)

                    if not is_trade_allowed(orig_t) or day_lock_flags.get(d, False) or is_last_day_pair_first or is_first_day_pair_second:
                        if is_last_day_pair_first:
                            log(f"🔒【月末保留保護】{d} の {p}さん({member_names.get(p, p)}) の『{orig_t}』は翌月連動なしのため固定化されました。")
                        elif is_first_day_pair_second:
                            log(f"🔒【月初保留保護】{d} の {p}さん({member_names.get(p, p)}) の『{orig_t}』は前月連動なしのため固定化されました。")
                            
                        for t in all_tasks:
                            if t != orig_t:
                                model.Add(x[p, d, t] == 0)
                        model.Add(x[p, d, orig_t] == 1)

            for p in existing_members:
                p_role = member_role.get(p, '')
                for d in dates:
                    if day_lock_flags.get(d, False):
                        continue
                    for t in all_tasks:
                        if not is_trade_allowed(t):
                            continue
                        if p_role == 'M' and t.endswith('C'):
                            model.Add(x[p, d, t] == 0)
                        elif p_role == 'C' and t.endswith('M'):
                            model.Add(x[p, d, t] == 0)

            for p in existing_members:
                p_gender = member_gender.get(p, '')
                if p_gender == 'F':
                    for d in dates:
                        if day_lock_flags.get(d, False):
                            continue
                        for t in all_tasks:
                            if not is_trade_allowed(t):
                                continue
                            if not is_female_allowed(t):
                                model.Add(x[p, d, t] == 0)

            for d in dates:
                if day_lock_flags.get(d, False):
                    continue
                tasks_today = [initial_assignment.get((p, d), '公休') for p in existing_members]
                for t in all_tasks:
                    if not is_trade_allowed(t):
                        continue
                    required_count = tasks_today.count(t)
                    model.Add(sum(x[p, d, t] for p in existing_members) == required_count)

            for d in dates:
                if day_lock_flags.get(d, False):
                    continue
                
                pair_swaps = {}
                for i in range(len(existing_members)):
                    for j in range(i + 1, len(existing_members)):
                        p1, p2 = existing_members[i], existing_members[j]
                        orig1 = initial_assignment.get((p1, d), '公休')
                        orig2 = initial_assignment.get((p2, d), '公休')
                        
                        p1_boundary_pair = is_boundary_pair_task(p1, d)
                        p2_boundary_pair = is_boundary_pair_task(p2, d)

                        if orig1 != orig2 and is_trade_allowed(orig1) and is_trade_allowed(orig2) and not p1_boundary_pair and not p2_boundary_pair:
                            s_var = model.NewBoolVar(f'pair_swap_{p1}_{p2}_{d}')
                            model.AddMinEquality(s_var, [x[p1, d, orig2], x[p2, d, orig1]])
                            pair_swaps[(p1, p2)] = s_var

                for p in existing_members:
                    orig_t = initial_assignment.get((p, d), '公休')
                    if not is_trade_allowed(orig_t) or is_boundary_pair_task(p, d):
                        continue
                    
                    p_swaps = []
                    for (p1, p2), s_var in pair_swaps.items():
                        if p == p1 or p == p2:
                            p_swaps.append(s_var)
                    
                    model.Add(sum(p_swaps) == 1 - x[p, d, orig_t])

            for d_idx in range(len(dates) - 1):
                d_curr = dates[d_idx]
                d_next = dates[d_idx + 1]

                for work_curr, work_next_required in pair_rules.items():
                    if work_curr in tasks_by_day[d_curr] and work_next_required in tasks_by_day[d_next]:
                        for p in existing_members:
                            model.Add(x[p, d_curr, work_curr] == x[p, d_next, work_next_required])

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

            objective_terms = []
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

                    for p1_idx in range(len(existing_members)):
                        for p2_idx in range(p1_idx + 1, len(existing_members)):
                            p1 = existing_members[p1_idx]
                            p2 = existing_members[p2_idx]

                            p1_t1, p1_t2, p1_t3 = initial_assignment.get((p1, d1)), initial_assignment.get((p1, d2)), initial_assignment.get((p1, d3))
                            p2_t1, p2_t2, p2_t3 = initial_assignment.get((p2, d1)), initial_assignment.get((p2, d2)), initial_assignment.get((p2, d3))

                            p1_is_triple = (p1_t1, p1_t2, p1_t3) in triple_rules and is_trade_allowed(p1_t1) and is_trade_allowed(p1_t2) and is_trade_allowed(p1_t3)
                            p2_is_triple = (p2_t1, p2_t2, p2_t3) in triple_rules and is_trade_allowed(p2_t1) and is_trade_allowed(p2_t2) and is_trade_allowed(p2_t3)

                            if p1_is_triple and p2_is_triple:
                                triple_swap_var = model.NewBoolVar(f'triple_swap_{p1}_{p2}_{d1}')
                                conds = [
                                    x[p1, d1, p2_t1], x[p1, d2, p2_t2], x[p1, d3, p2_t3],
                                    x[p2, d1, p1_t1], x[p2, d2, p1_t2], x[p2, d3, p1_t3]
                                ]
                                model.AddMinEquality(triple_swap_var, conds)
                                objective_terms.append(triple_swap_var * -100000)
                                triple_trade_vars.append((p1, p2, d1, d2, d3, (p1_t1, p1_t2, p1_t3), (p2_t1, p2_t2, p2_t3), triple_swap_var))

            LATE_EARLY_PENALTY_WEIGHT = 20

            for p in existing_members:
                for d_idx in range(len(dates) - 1):
                    d_curr = dates[d_idx]
                    d_next = dates[d_idx + 1]

                    for t_curr in all_tasks:
                        if task_end_type.get(t_curr) == 'LATE':
                            for t_next in all_tasks:
                                if task_start_type.get(t_next) == 'EARLY':
                                    late_early_var = model.NewBoolVar(f'late_early_{p}_{d_curr}_{t_curr}_{t_next}')
                                    model.AddMinEquality(late_early_var, [x[p, d_curr, t_curr], x[p, d_next, t_next]])
                                    objective_terms.append(late_early_var * LATE_EARLY_PENALTY_WEIGHT)

            LOAD_DIFF_PENALTY_WEIGHT = 20

            for d in dates:
                if day_lock_flags.get(d, False):
                    continue
                for i in range(len(existing_members)):
                    for j in range(i + 1, len(existing_members)):
                        p1, p2 = existing_members[i], existing_members[j]
                        orig1 = initial_assignment.get((p1, d), '公休')
                        orig2 = initial_assignment.get((p2, d), '公休')

                        if orig1 != orig2 and is_trade_allowed(orig1) and is_trade_allowed(orig2):
                            l1 = task_load_map.get(orig1, 0)
                            l2 = task_load_map.get(orig2, 0)

                            if (l1 == 1 and l2 == 3) or (l1 == 3 and l2 == 1):
                                load_diff_var = model.NewBoolVar(f'load_diff_{p1}_{p2}_{d}')
                                model.AddMinEquality(load_diff_var, [x[p1, d, orig2], x[p2, d, orig1]])
                                objective_terms.append(load_diff_var * LOAD_DIFF_PENALTY_WEIGHT)

            member_overflow_vars = {}
            for p in existing_members:
                p_base_area = member_base_area.get(p, 'ANY')
                p_of_terms = []
                
                if p_base_area != 'ANY':
                    for d in dates:
                        if day_lock_flags.get(d, False):
                            continue
                        
                        if is_boundary_pair_task(p, d):
                            continue

                        for t in all_tasks:
                            if not is_trade_allowed(t):
                                continue
                            t_area = get_task_area(t)
                            if t_area != 'ANY' and t_area != p_base_area:
                                p_of_terms.append(x[p, d, t])
                
                of_var = model.NewIntVar(0, len(dates), f'overflow_{p}')
                model.Add(of_var == sum(p_of_terms))
                member_overflow_vars[p] = of_var

                past_of = member_past_overflow.get(p, 0)
                weighted_penalty = 1000 + (past_of * 500)
                objective_terms.append(of_var * weighted_penalty)

            max_overflow_var = model.NewIntVar(0, len(dates), 'max_overflow')
            for p in existing_members:
                model.Add(max_overflow_var >= member_overflow_vars[p])
            
            objective_terms.append(max_overflow_var * 5000)

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
            solver.parameters.max_time_in_seconds = 60.0
            status = solver.Solve(model)
            status_name = solver.StatusName(status)
            log(f"ソルバー実行完了 Status: {status_name}")

            change_logs = []
            triple_applied_logs = []
            pair_applied_logs = []
            changed_cells = set()
            overflow_cells = set()

            if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
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
                            f"【3連番一括トレード成立】{d1}〜{d3} : {name1}さん ({p1_t1}->{p1_t2}->{p1_t3}) 🔁 {name2}さん ({p2_t1}->{p2_t2}->{p2_t3})"
                        )

                result_rows = []
                
                if not daytype_row.empty:
                    r_dict = daytype_row.iloc[0].to_dict()
                    r_dict['OverFlow'] = ''
                    r_dict['3M_Total_OF'] = ''
                    result_rows.append(r_dict)
                if not lock_row.empty:
                    r_dict = lock_row.iloc[0].to_dict()
                    r_dict['OverFlow'] = ''
                    r_dict['3M_Total_OF'] = ''
                    result_rows.append(r_dict)

                for p in existing_members:
                    p_base_area = member_base_area.get(p, 'ANY')
                    overflow_count = 0
                    
                    row_src = df_initial_indexed.loc[p]
                    row = {
                        id_col_name: p,
                        name_col_name: member_names.get(p, '')
                    }

                    if col_m1:
                        row[col_m1] = parse_int_safely(row_src.get(col_m1, 0))
                    if col_m2:
                        row[col_m2] = parse_int_safely(row_src.get(col_m2, 0))

                    for d in dates:
                        task_assigned = final_schedule.get((p, d), initial_assignment.get((p, d), '公休'))
                        row[d] = task_assigned
                        
                        if not day_lock_flags.get(d, False) and is_trade_allowed(task_assigned) and not is_boundary_pair_task(p, d):
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

                for d_idx in range(len(dates) - 1):
                    d_curr = dates[d_idx]
                    d_next = dates[d_idx + 1]
                    for p in existing_members:
                        work_curr = final_schedule.get((p, d_curr), '公休')
                        if work_curr in pair_rules:
                            work_next = pair_rules[work_curr]
                            p_name = member_names.get(p, p)
                            pair_applied_logs.append(
                                f"【ペア整合確認】{p_name}さん({p}): {d_curr}『{work_curr}』 ➔ {d_next}『{work_next}』(完全連動)"
                            )
                
                return df_result, True, "OK", change_logs, pair_applied_logs, triple_applied_logs, changed_cells, overflow_cells, id_col_name, day_lock_flags, debug_logs
            else:
                return df_initial_raw, False, f"Solver Status: {status_name}", [], [], [], set(), set(), id_col_name, {}, debug_logs

        except Exception as e:
            err_msg = traceback.format_exc()
            log("❌ プログラム実行中に予期せぬエラーが発生しました:")
            log(err_msg)
            return df_initial_raw, False, f"Exception: {str(e)}", [], [], [], set(), set(), "", {}, debug_logs

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("計算中..."):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg, change_logs, pair_debug_logs, triple_logs, changed_cells, overflow_cells, id_col, day_lock_flags, debug_logs = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("最適化計算が完了しました！")

                    if triple_logs:
                        st.subheader("🔥 優先適用された【3連番一括トレード】")
                        for tlog in triple_logs:
                            st.success(tlog)

                    if change_logs:
                        with st.expander(f"📋 変更（トレード）された勤務一覧 ({len(change_logs)}件)", expanded=False):
                            for clog in change_logs:
                                st.write(clog)
                    else:
                        st.info("ℹ️ 初期シフトから変更の必要はありませんでした。（全ての勤務が自エリアと一致しています）")

                    with st.expander("🔍 適用されたペア制約（2日連動）ログ", expanded=False):
                        for p_log in sorted(list(set(pair_debug_logs))):
                            st.write(p_log)

                    st.subheader("📊 最適化結果プレビュー")
                    st.caption("※ **薄ピンク色の列**: LOCK（固定指定）された日")
                    st.caption("※ **黄緑色のセル**: トレードにより変更された勤務")
                    st.caption("※ **黄色のセル**: 溢れ（自エリアと不一致・かつトレード対象）が発生している勤務")
                    st.caption("※ **赤文字のセル**: 週休・休暇・公休などの休日セル（白背景＋赤文字）")

                    OFF_KEYWORDS = ['週休', '休暇', '公休', '有休', '特休', '代休', 'OFF', '明']

                    def highlight_schedule(df):
                        style_df = pd.DataFrame('', index=df.index, columns=df.columns)
                        
                        for idx, row in df.iterrows():
                            p_id = str(row[id_col])
                            for col in df.columns:
                                cell_val = str(row[col])
                                str_col = str(col)
                                is_locked = day_lock_flags.get(str_col, False)
                                is_changed = (p_id, str_col) in changed_cells
                                is_overflow = (p_id, str_col) in overflow_cells
                                
                                is_off = any(kw in cell_val for kw in OFF_KEYWORDS)

                                if is_off:
                                    bg_color = '#f8d7da' if is_locked else '#ffffff'
                                    style_df.loc[idx, col] = f'background-color: {bg_color}; color: #d9534f; font-weight: bold;'
                                elif is_locked:
                                    style_df.loc[idx, col] = 'background-color: #f8d7da; color: #721c24;'
                                elif is_overflow:
                                    style_df.loc[idx, col] = 'background-color: #fff3cd; color: #856404; font-weight: bold;'
                                elif is_changed:
                                    style_df.loc[idx, col] = 'background-color: #d4edda; color: #155724; font-weight: bold;'
                                    
                        return style_df

                    styled_df = result_df.style.apply(highlight_schedule, axis=None)
                    st.dataframe(styled_df)

                    def generate_styled_html(df, id_col_name, day_lock_flags, changed_cells, overflow_cells):
                        html = """
                        <html>
                        <head>
                            <meta charset="utf-8">
                            <style>
                                body { font-family: 'Helvetica Neue', Arial, sans-serif; padding: 20px; }
                                h2 { color: #333; }
                                table { border-collapse: collapse; width: 100%; font-size: 11px; }
                                th, td { border: 1px solid #ddd; padding: 6px; text-align: center; white-space: nowrap; }
                                th { background-color: #f2f2f2; color: #333; }
                            </style>
                        </head>
                        <body>
                            <h2>勤務変更補助システム - 最適化結果</h2>
                            <table>
                                <thead>
                                    <tr>
                        """
                        for col in df.columns:
                            html += f"<th>{col}</th>"
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
                                color = "#000000"
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

                                html += f'<td style="background-color: {bg}; color: {color}; font-weight: {weight};">{cell_val}</td>'
                            html += "</tr>"
                        html += "</tbody></table></body></html>"
                        return html

                    col_dl1, col_dl2 = st.columns(2)

                    with col_dl1:
                        csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                        st.download_button(
                            label="📥 CSVファイルをダウンロード",
                            data=csv_data,
                            file_name="Optimized_Schedule.csv",
                            mime="text/csv",
                            use_container_width=True
                        )

                    with col_dl2:
                        html_data = generate_styled_html(result_df, id_col, day_lock_flags, changed_cells, overflow_cells)
                        st.download_button(
                            label="📄 色付きHTML（PDF保存用）をダウンロード",
                            data=html_data.encode('utf-8-sig'),
                            file_name="Optimized_Schedule.html",
                            mime="text/html",
                            use_container_width=True
                        )

                else:
                    st.error(f"解が見つからなかったか、エラーが発生しました。（詳細: {log_msg}）")

                with st.expander("🐛 実行・デバッグログ（トラブルシューティング用）", expanded=not success):
                    st.code("\n".join(debug_logs), language="text")

                # 🔥 現在実装されている機能・ロジック一覧（アコーディオン）
                with st.expander("📌 現在実装されている機能・ロジック一覧", expanded=False):
                    st.markdown("""
                    ### 1. ハード制約（絶対遵守ルール）
                    * **同日1勤務制約**: 1人のメンバーが同じ日に複数の仕業を担当することはできません。
                    * **仕業必要数保持**: 毎日の各仕業の必要人数（初期勤務表上の件数）を厳示してトレードを行います。
                    * **役職（M/C）不可制約**: M役職はC専用仕業（末尾C）を担当不可。C役職はM専用仕業（末尾M）を担当不可。
                    * **女性保護制約**: 女性メンバー（Gender='F'）は `FemaleAllowed='N'` の仕業を担当不可。
                    * **不可トレード保護**: `TradeAllowed='N'`（または英字コード等の固定勤務）および休日・休暇類は固定。
                    * **LOCK行（日付固定）保護**: 初期勤務表の LOCK 行で指定された日付は全メンバー変更不可。
                    * **エリアミスマッチトレード禁止**: BaseArea 以外の異エリア仕業へ自発的に移るトレードは禁止。
                    * **2日連動ペア完全保持**: 宿泊勤務等のペア仕業（1日目➔2日目）は、トレード時もペア関係を完全維持。
                    * **月末月初境界ペアの強制固定**: 
                        * 月末日の泊まり1日目（翌月連動なし）は強制固定。
                        * 月初日の泊まり2日目（前月連動なし）は強制固定。

                    ---

                    ### 2. ソフト制約・評価関数（最適化ペナルティ / 優先度）
                    * **3連番一括トレード（インセンティブ: -100,000）**:
                        * 3日連続の連動仕業ペアを一括で相互トレードした場合、非常に大きな負のペナルティ（優先ボーナス）を与えて積極的に成立させます。
                    * **溢れ数（OverFlow）最小化（ペナルティ: 1,000 + 過去累計×500）**:
                        * 自エリア外勤務の割当（溢れ）を極力排除。過去2ヶ月の累計（`OF_M1`+`OF_M2`）が多いメンバーほどペナルティを重くし、過去実績を含めた長期的平準化を図ります。
                        * **※月末月初の境界保護で固定された泊まり勤務片割れは溢れカウントから除外**されます。
                    * **最大溢れ数の平準化（ペナルティ: 5,000）**:
                        * 特定の個人に溢れが集中しないよう、メンバー内の最大溢れ数（`Max OverFlow`）を強力に抑制します。
                    * **遅出➔早出パターンの防止（ペナルティ: 20）**:
                        * 前日 EndType='LATE' の直後に翌日 StartType='EARLY' が発生する過酷シフトを抑制します。
                    * **Load不均衡トレードの防止（ペナルティ: 20）**:
                        * Load 1（軽）と Load 3（重）の不公平な相互トレードにペナルティを与え、勤務強度のアンバランスを回避します。
                    * **トレード回数最小化（ペナルティ: 1）**:
                        * 不要な勤務の変更を避け、初期シフトからの変更数を最小限に抑えます。

                    ---

                    ### 3. データ集計・表示・出力機能
                    * **過去溢れ継承**: 初期勤務表の `OF_M1` / `OF_M2` 列を自動認識し、今月分と合わせた「3M_Total_OF（3ヶ月合計溢れ数）」を自動算出。
                    * **視認性カラーハイライト**:
                        * 薄ピンク色：LOCK（固定）日付
                        * 黄緑色：トレード変更セル
                        * 黄色：溢れ（自エリア外）セル
                        * 赤文字：週休・公休・休暇等の休日セル
                    * **スマートログ格納**: トレード一覧、ペア連動ログ、デバッグログを折りたたみ表示（`expander`）に整理。
                    * **多角化ダウンロード**: UTF-8 BOM付き `CSVダウンロード` と、色付きスタイルを保持した `PDF保存用HTMLダウンロード` に対応。
                    """)