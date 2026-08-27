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
    return str(val).strip().upper()

def normalize_area_dynamic(val):
    s = clean_str(val)
    return s if s else 'ANY'

def is_fixed_task(task_code):
    if not task_code or task_code == 'OFF':
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

        # 1. メンバーマスターの動的パース
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

        # 2. 仕業マスターの動的パース
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

        # 3. 初期勤務表データの整理
        df_sched = df_initial_raw[df_initial_raw[id_col_name].apply(clean_str) != 'DAYTYPE'].copy()
        df_sched[id_col_name] = df_sched[id_col_name].apply(clean_str)

        member_names = {}
        for _, row in df_sched.iterrows():
            m_id = clean_str(row[id_col_name])
            m_name = str(row[name_col_name]).strip() if pd.notna(row[name_col_name]) else m_id
            member_names[m_id] = m_name

        df_initial_indexed = df_sched.set_index(id_col_name)
        
        existing_members = [m for m in members if m in df_initial_indexed.index]
        if len(existing_members) == 0:
            return df_initial_raw, False, "メンバーIDが一致しませんでした", [], [], [], [], set(), ""

        initial_assignment = {}
        all_tasks_set = set(['OFF'])

        for p in existing_members:
            for d in dates:
                val = clean_str(df_initial_indexed.loc[p, d])
                if not val:
                    val = 'OFF'
                initial_assignment[(p, d)] = val
                all_tasks_set.add(val)

        all_tasks = list(all_tasks_set)

        model = cp_model.CpModel()
        x = {}
        for p in existing_members:
            for d in dates:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

        # -------------------------------------------------------------
        # ハード制約
        # -------------------------------------------------------------

        # 1. 1人1日1仕業
        for d in dates:
            for p in existing_members:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

        # 2. OFF・特殊仕業の固定
        for p in existing_members:
            for d in dates:
                orig_t = initial_assignment.get((p, d), 'OFF')
                if is_fixed_task(orig_t):
                    for t in all_tasks:
                        if t != orig_t:
                            model.Add(x[p, d, t] == 0)
                    model.Add(x[p, d, orig_t] == 1)

        # 3. 役職マッチング
        for p in existing_members:
            p_role = member_role.get(p, '')
            for d in dates:
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
                    for t in all_tasks:
                        if is_fixed_task(t):
                            continue
                        if not is_female_allowed(t):
                            model.Add(x[p, d, t] == 0)

        # 5. 各日の仕業人数の維持
        for d in dates:
            tasks_today = [initial_assignment.get((p, d), 'OFF') for p in existing_members]
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

        # 7. 【絶対禁忌ルール】他エリア仕業への新規割り当て禁止（動的エリア判定）
        for p in existing_members:
            p_base_area = member_base_area.get(p, 'ANY')
            if p_base_area != 'ANY':
                for d in dates:
                    orig_t = initial_assignment.get((p, d), 'OFF')
                    for t in all_tasks:
                        if is_fixed_task(t) or t == orig_t:
                            continue
                        t_area = get_task_area(t)
                        if t_area != 'ANY' and t_area != p_base_area:
                            model.Add(x[p, d, t] == 0)

        # -------------------------------------------------------------
        # 目的関数: 自エリア一致の最大化 ＆ 変更回数の最小化
        # -------------------------------------------------------------
        objective_terms = []

        for p in existing_members:
            p_base_area = member_base_area.get(p, 'ANY')
            for d in dates:
                orig_t = initial_assignment.get((p, d), 'OFF')
                for t in all_tasks:
                    if is_fixed_task(t):
                        continue
                    
                    t_area = get_task_area(t)
                    
                    if p_base_area != 'ANY' and t_area == p_base_area:
                        objective_terms.append(x[p, d, t] * -10000)
                    
                    if t != orig_t:
                        objective_terms.append(x[p, d, t] * 1)

        model.Minimize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60.0
        status = solver.Solve(model)

        change_logs = []
        pair_applied_logs = []
        changed_cells = set()

        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            final_schedule = {}
            for d in dates:
                for p in existing_members:
                    for t in all_tasks:
                        if solver.Value(x[p, d, t]) == 1:
                            final_schedule[(p, d)] = t
                            orig_t = initial_assignment.get((p, d), 'OFF')
                            if t != orig_t:
                                p_name = member_names.get(p, p)
                                change_logs.append(f"【{d}】{p_name}さん({p}) : {orig_t} ➔ {t}")
                                changed_cells.add((p, d))
                            break

            # 結果データフレームの作成と OverFlow カウント計算
            result_rows = []
            for p in existing_members:
                p_base_area = member_base_area.get(p, 'ANY')
                overflow_count = 0
                
                row = {
                    id_col_name: p,
                    name_col_name: member_names.get(p, '')
                }
                for d in dates:
                    task_assigned = final_schedule.get((p, d), 'OFF')
                    row[d] = task_assigned
                    
                    # 溢れ判定（BaseAreaとTaskAreaが不一致、かつ両方ANYでない場合）
                    t_area = get_task_area(task_assigned)
                    if p_base_area != 'ANY' and t_area != 'ANY' and p_base_area != t_area:
                        overflow_count += 1

                # 最終列に OverFlow を追加
                row['OverFlow'] = overflow_count
                result_rows.append(row)

            df_result = pd.DataFrame(result_rows)

            for d_idx in range(len(dates) - 1):
                d_curr = dates[d_idx]
                d_next = dates[d_idx + 1]
                for p in existing_members:
                    work_curr = final_schedule.get((p, d_curr), 'OFF')
                    if work_curr in pair_rules:
                        work_next = pair_rules[work_curr]
                        p_name = member_names.get(p, p)
                        pair_applied_logs.append(
                            f"【ペア整合確認】{p_name}さん({p}): {d_curr}『{work_curr}』 ➔ {d_next}『{work_next}』(完全連動)"
                        )
            
            return df_result, True, "OK", change_logs, pair_applied_logs, [], [], changed_cells, id_col_name
        else:
            return df_initial_raw, False, f"Solver Status: {solver.StatusName(status)}", [], [], [], [], set(), ""

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("計算中..."):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg, change_logs, pair_debug_logs, _, _, changed_cells, id_col = run_optimization(df_m, df_t, df_i)
                
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
                    st.caption("※ 初期シフトから変更された箇所は **黄緑色** にハイライトされます")

                    def highlight_changes(df):
                        style_df = pd.DataFrame('', index=df.index, columns=df.columns)
                        for idx, row in df.iterrows():
                            p_id = str(row[id_col])
                            for col in df.columns:
                                if (p_id, str(col)) in changed_cells:
                                    style_df.loc[idx, col] = 'background-color: #d4edda; color: #155724; font-weight: bold;'
                        return style_df

                    styled_df = result_df.style.apply(highlight_changes, axis=None)
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