import streamlit as st
import pandas as pd
import re
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

def normalize_area_dynamic(val):
    s = clean_str(val)
    return s if s else 'ANY'

def is_fixed_task(task_code):
    if not task_code:
        return True
    if not task_code[0].isdigit():
        return True
    return False

if check_password():
    st.title("勤務変更補助システム")
    st.caption("自動シフトトレード・エリア最適化ソルバー")

    st.subheader("1. データファイルのアップロード")
    file_members = st.file_uploader("メンバーマスター (Member_Master.csv)", type=["csv"])
    file_tasks = st.file_uploader("仕業マスター (Task_Master.csv)", type=["csv"])
    file_initial = st.file_uploader("初期勤務表 (Initial_Schedule.csv)", type=["csv"])

    def run_optimization(df_members, df_tasks, df_initial_raw):
        id_col_name = df_initial_raw.columns[0]
        name_col_name = df_initial_raw.columns[1]
        dates = [clean_str(c) for c in df_initial_raw.columns[2:]]

        # -------------------------------------------------------------
        # 1. 制御行（LOCK行）および DAYTYPE行 の判定
        # -------------------------------------------------------------
        lock_row = df_initial_raw[df_initial_raw[id_col_name].apply(clean_str) == 'LOCK']
        day_lock_flags = {}
        for d in dates:
            if not lock_row.empty:
                val = clean_str(lock_row.iloc[0][d])
                day_lock_flags[d] = val in ['LOCK', '1', 'YES', 'TRUE', '固定']
            else:
                day_lock_flags[d] = False

        daytype_row = df_initial_raw[df_initial_raw[id_col_name].apply(clean_str) == 'DAYTYPE']

        # -------------------------------------------------------------
        # 2. メンバーマスターの動的パース
        # -------------------------------------------------------------
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

        # -------------------------------------------------------------
        # 3. 仕業マスターの動的パース
        # -------------------------------------------------------------
        task_area_map = {}
        task_female_allowed_map = {}
        pair_rules = {}

        if 'TaskID' in df_tasks.columns:
            for _, row in df_tasks.iterrows():
                t_id = clean_str(row['TaskID'])
                t_area = normalize_area_dynamic(row.get('TargetArea', ''))
                f_allowed = clean_str(row.get('FemaleAllowed', 'Y'))
                pair_id = clean_str(row.get('PairTaskID', ''))

                task_area_map[t_id] = t_area
                task_female_allowed_map[t_id] = f_allowed

                m_match = re.match(r'^(\d+)([MC])$', t_id)
                if m_match:
                    num_str = m_match.group(1)
                    role_char = m_match.group(2)
                    task_area_map[t_id] = t_area
                    task_female_allowed_map[t_id] = f_allowed

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

                    if pair_id and pair_id.isdigit():
                        prev_plain_id = f"{pair_id}{role_char}"
                        pair_rules[prev_plain_id] = plain_id

        for _, row in df_tasks.iterrows():
            t_id = clean_str(row['TaskID'])
            pair_id = clean_str(row.get('PairTaskID', ''))
            if pair_id and not pair_id.isdigit():
                pair_rules[pair_id] = t_id

        def get_task_area(task_code):
            if is_fixed_task(task_code):
                return 'ANY'
            return task_area_map.get(task_code, 'ANY')

        def is_female_allowed(task_code):
            return task_female_allowed_map.get(task_code, 'Y') != 'N'

        # -------------------------------------------------------------
        # 4. 初期勤務表データの整理
        # -------------------------------------------------------------
        ignored_rows = ['DAYTYPE', 'LOCK']
        df_sched = df_initial_raw[~df_initial_raw[id_col_name].apply(clean_str).isin(ignored_rows)].copy()
        df_sched[id_col_name] = df_sched[id_col_name].apply(clean_str)

        member_names = {}
        for _, row in df_sched.iterrows():
            m_id = clean_str(row[id_col_name])
            m_name = str(row[name_col_name]).strip() if pd.notna(row[name_col_name]) else m_id
            member_names[m_id] = m_name

        df_initial_indexed = df_sched.set_index(id_col_name)
        
        existing_members = [m for m in members if m in df_initial_indexed.index]
        if len(existing_members) == 0:
            return df_initial_raw, False, "メンバーIDが一致しませんでした", [], [], [], [], set(), set(), "", {}

        initial_assignment = {}
        all_tasks_set = set()

        for p in existing_members:
            for d in dates:
                val = str(df_initial_indexed.loc[p, d]).strip() if pd.notna(df_initial_indexed.loc[p, d]) else '公休'
                if not val:
                    val = '公休'
                initial_assignment[(p, d)] = val
                all_tasks_set.add(val)

        all_tasks = list(all_tasks_set)

        # -------------------------------------------------------------
        # 5. OR-Tools モデル構築
        # -------------------------------------------------------------
        model = cp_model.CpModel()
        x = {}
        for p in existing_members:
            for d in dates:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

        # 1. 1人1日1仕業
        for d in dates:
            for p in existing_members:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

        # 2. OFF・公休・休暇等の固定 ＆ LOCK日の全員固定
        for p in existing_members:
            for d in dates:
                orig_t = initial_assignment.get((p, d), '公休')
                if is_fixed_task(orig_t) or day_lock_flags.get(d, False):
                    for t in all_tasks:
                        if t != orig_t:
                            model.Add(x[p, d, t] == 0)
                    model.Add(x[p, d, orig_t] == 1)

        # 3. 役職マッチング
        for p in existing_members:
            p_role = member_role.get(p, '')
            for d in dates:
                if day_lock_flags.get(d, False):
                    continue
                for t in all_tasks:
                    if is_fixed_task(t):
                        continue
                    if p_role == 'M' and t.endswith('C'):
                        model.Add(x[p, d, t] == 0)
                    elif p_role == 'C' and t.endswith('M'):
                        model.Add(x[p, d, t] == 0)

        # 4. 女性不可仕業ガード
        for p in existing_members:
            p_gender = member_gender.get(p, '')
            if p_gender == 'F':
                for d in dates:
                    if day_lock_flags.get(d, False):
                        continue
                    for t in all_tasks:
                        if is_fixed_task(t):
                            continue
                        if not is_female_allowed(t):
                            model.Add(x[p, d, t] == 0)

        # 5. 各日の仕業人数の維持
        for d in dates:
            if day_lock_flags.get(d, False):
                continue
            tasks_today = [initial_assignment.get((p, d), '公休') for p in existing_members]
            for t in all_tasks:
                if is_fixed_task(t):
                    continue
                required_count = tasks_today.count(t)
                model.Add(sum(x[p, d, t] for p in existing_members) == required_count)

        # 6. 日跨ぎペア制約
        for d_idx in range(len(dates) - 1):
            d_curr = dates[d_idx]
            d_next = dates[d_idx + 1]

            for work_curr, work_next_required in pair_rules.items():
                if work_curr in all_tasks and work_next_required in all_tasks:
                    for p in existing_members:
                        model.Add(x[p, d_curr, work_curr] == x[p, d_next, work_next_required])

        # 7. 他エリア仕業への新規割り当て禁止
        for p in existing_members:
            p_base_area = member_base_area.get(p, 'ANY')
            if p_base_area != 'ANY':
                for d in dates:
                    if day_lock_flags.get(d, False):
                        continue
                    orig_t = initial_assignment.get((p, d), '公休')
                    for t in all_tasks:
                        if is_fixed_task(t) or t == orig_t:
                            continue
                        t_area = get_task_area(t)
                        if t_area != 'ANY' and t_area != p_base_area:
                            model.Add(x[p, d, t] == 0)

        # -------------------------------------------------------------
        # 目的関数
        # -------------------------------------------------------------
        objective_terms = []

        for p in existing_members:
            p_base_area = member_base_area.get(p, 'ANY')
            for d in dates:
                if day_lock_flags.get(d, False):
                    continue

                orig_t = initial_assignment.get((p, d), '公休')
                for t in all_tasks:
                    if is_fixed_task(t):
                        continue
                    
                    t_area = get_task_area(t)
                    
                    if p_base_area != 'ANY' and t_area == p_base_area:
                        objective_terms.append(x[p, d, t] * -10000)
                    
                    if t != orig_t:
                        objective_terms.append(x[p, d, t] * 1)

        model.Minimize(sum(objective_terms))

        # -------------------------------------------------------------
        # ソルバー実行
        # -------------------------------------------------------------
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60.0
        status = solver.Solve(model)

        change_logs = []
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

            result_rows = []
            
            if not daytype_row.empty:
                r_dict = daytype_row.iloc[0].to_dict()
                r_dict['OverFlow'] = ''
                result_rows.append(r_dict)
            if not lock_row.empty:
                r_dict = lock_row.iloc[0].to_dict()
                r_dict['OverFlow'] = ''
                result_rows.append(r_dict)

            for p in existing_members:
                p_base_area = member_base_area.get(p, 'ANY')
                overflow_count = 0
                
                row = {
                    id_col_name: p,
                    name_col_name: member_names.get(p, '')
                }
                for d in dates:
                    task_assigned = final_schedule.get((p, d), initial_assignment.get((p, d), '公休'))
                    row[d] = task_assigned
                    
                    # 溢れ判定
                    if not day_lock_flags.get(d, False):
                        t_area = get_task_area(task_assigned)
                        if p_base_area != 'ANY' and t_area != 'ANY' and p_base_area != t_area:
                            overflow_count += 1
                            overflow_cells.add((p, d))

                row['OverFlow'] = int(overflow_count)
                result_rows.append(row)

            df_result = pd.DataFrame(result_rows)

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
            
            return df_result, True, "OK", change_logs, pair_applied_logs, [], [], changed_cells, overflow_cells, id_col_name, day_lock_flags
        else:
            return df_initial_raw, False, f"Solver Status: {solver.StatusName(status)}", [], [], [], [], set(), set(), "", {}

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("計算中..."):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg, change_logs, pair_debug_logs, _, _, changed_cells, overflow_cells, id_col, day_lock_flags = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("最適化計算が完了しました！")
                    if change_logs:
                        st.subheader("📋 変更（トレード）された勤務一覧")
                        for clog in change_logs:
                            st.write(clog)
                    else:
                        st.info("ℹ️ 初期シフトから変更の必要はありませんでした。（全ての勤務が自エリアと一致しています）")

                    with st.expander("🔍 適用されたペア制約ログ"):
                        for p_log in sorted(list(set(pair_debug_logs))):
                            st.write(p_log)

                    st.subheader("📊 最適化結果プレビュー")
                    st.caption("※ **薄ピンク色の列**: LOCK（固定指定）された日")
                    st.caption("※ **黄緑色のセル**: トレードにより変更された勤務")
                    st.caption("※ **黄色のセル**: 溢れ（自エリアと不一致）が発生している勤務")
                    st.caption("※ **赤文字のセル**: 週休・休暇・公休などの休日セル（白背景＋赤文字）")

                    # 休日・休暇判定用キーワードリスト
                    OFF_KEYWORDS = ['週休', '休暇', '公休', '有休', '特休', '代休', 'OFF']

                    # スタイル適用関数（背景色：白背景＋赤太字）
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
                                
                                # 休日・休暇系の文字が含まれているか判定
                                is_off = any(kw in cell_val for kw in OFF_KEYWORDS)

                                # 1. 週休・休暇などは白背景＋赤文字太字
                                if is_off:
                                    # LOCK列の中にある場合でも背景はLOCK色を保ちつつ赤文字にするか、白背景＋赤文字にするか
                                    bg_color = '#f8d7da' if is_locked else '#ffffff'
                                    style_df.loc[idx, col] = f'background-color: {bg_color}; color: #d9534f; font-weight: bold;'
                                # 2. LOCK指定の日（列全体を薄ピンクに）
                                elif is_locked:
                                    style_df.loc[idx, col] = 'background-color: #f8d7da; color: #721c24;'
                                # 3. 溢れセル（黄色に）
                                elif is_overflow:
                                    style_df.loc[idx, col] = 'background-color: #fff3cd; color: #856404; font-weight: bold;'
                                # 4. トレード変更セル（黄緑に）
                                elif is_changed:
                                    style_df.loc[idx, col] = 'background-color: #d4edda; color: #155724; font-weight: bold;'
                                    
                        return style_df

                    styled_df = result_df.style.apply(highlight_schedule, axis=None)
                    st.dataframe(styled_df)

                    csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="Optimized_Schedule.csv をダウンロード",
                        data=csv_data,
                        file_name="Optimized_Schedule.csv",
                        mime="text/csv"
                    )
                else:
                    st.error(f"解が見つかりませんでした。（詳細: {log_msg}）")
        else:
            st.error("エラー: 3つのファイルをすべてアップロードしてください。")