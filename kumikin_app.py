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

if check_password():
    st.title("勤務変更補助システム")
    st.caption("自動シフトトレード・エリア最適化ソルバー")

    st.subheader("1. データファイルのアップロード")
    file_members = st.file_uploader("メンバーマスター (Member_Master.csv)", type=["csv"])
    file_tasks = st.file_uploader("仕業マスター (Task_Master.csv)", type=["csv"])
    file_initial = st.file_uploader("初期勤務表 (Initial_Schedule.csv)", type=["csv"])

    def run_optimization(df_members, df_tasks, df_initial_raw):
        # 列の解析: 1列目=MemberID, 2列目=Name, 3列目以降=日付
        id_col_name = df_initial_raw.columns[0]
        name_col_name = df_initial_raw.columns[1]
        dates = [clean_str(c) for c in df_initial_raw.columns[2:]]

        # -------------------------------------------------------------
        # 1. メンバーマスターのパース (BaseArea & Role 取得)
        # -------------------------------------------------------------
        df_members['MemberID'] = df_members['MemberID'].apply(clean_str)
        member_base_area = {}
        member_role = {}
        
        for _, row in df_members.iterrows():
            m_id = clean_str(row['MemberID'])
            area = clean_str(row.get('BaseArea', ''))
            role = clean_str(row.get('Role', ''))
            member_base_area[m_id] = area
            member_role[m_id] = role

        members = list(member_base_area.keys())

        # -------------------------------------------------------------
        # 2. 仕業マスターのパース
        # -------------------------------------------------------------
        task_area_map = {}
        if 'TaskID' in df_tasks.columns and 'TargetArea' in df_tasks.columns:
            for _, row in df_tasks.iterrows():
                t_id = clean_str(row['TaskID'])
                t_area = clean_str(row['TargetArea'])
                
                m = re.match(r'([MC])_(\d+)', t_id)
                if m:
                    prefix = m.group(1)
                    num = m.group(2)
                    task_area_map[(num, prefix)] = t_area

        def get_task_area(task_code):
            if not task_code or task_code == 'OFF':
                return 'ANY'
            m = re.match(r'(\d+)([MC])', task_code)
            if m:
                num = m.group(1)
                prefix = m.group(2)
                return task_area_map.get((num, prefix), 'ANY')
            m = re.match(r'([MC])(\d+)', task_code)
            if m:
                prefix = m.group(1)
                num = m.group(2)
                return task_area_map.get((num, prefix), 'ANY')
            return 'ANY'

        # -------------------------------------------------------------
        # 3. 初期勤務表データの整理
        # -------------------------------------------------------------
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
            return df_initial_raw, False, "メンバーIDが一致しませんでした", [], [], [], []

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

        pair_rules = {
            "11C": "12C", "11M": "12M",
            "15C": "16C", "15M": "16M",
            "18C": "19C", "18M": "19M",
            "25C": "26C", "25M": "26M",
            "32C": "33C", "32M": "33M",
            "39C": "40C", "39M": "40M",
            "46C": "47C", "46M": "47M",
            "53C": "54C", "53M": "54M",
            "4C":  "5C",  "4M":  "5M",
            "60C": "61C", "60M": "61M",
            "67C": "68C", "67M": "68M",
            "74C": "75C", "74M": "75M"
        }

        model = cp_model.CpModel()
        x = {}
        for p in existing_members:
            for d in dates:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

        # -------------------------------------------------------------
        # 制約条件の設定
        # -------------------------------------------------------------

        # 1. 1人1日1仕業
        for d in dates:
            for p in existing_members:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

        # 2. 初期シフトで OFF の日は絶対に OFF
        for p in existing_members:
            for d in dates:
                orig_t = initial_assignment.get((p, d), 'OFF')
                if orig_t == 'OFF':
                    for t in all_tasks:
                        if t != 'OFF':
                            model.Add(x[p, d, t] == 0)
                    model.Add(x[p, d, 'OFF'] == 1)

        # ★【追加】3. 役職（Role）マッチング制約 (ハード制約)
        for p in existing_members:
            p_role = member_role.get(p, '')
            for d in dates:
                for t in all_tasks:
                    if t in ['OFF', 'A1', 'S2', 'S3', 'J5', 'J6']:
                        continue
                    # Role: M の人は C仕業（末尾C）を担当禁止
                    if p_role == 'M' and t.endswith('C'):
                        model.Add(x[p, d, t] == 0)
                    # Role: C の人は M仕業（末尾M）を担当禁止
                    elif p_role == 'C' and t.endswith('M'):
                        model.Add(x[p, d, t] == 0)

        # 4. 各日の出勤仕業の人数（需要数）を維持
        for d in dates:
            tasks_today = [initial_assignment.get((p, d), 'OFF') for p in existing_members]
            for t in all_tasks:
                if t == 'OFF':
                    continue
                required_count = tasks_today.count(t)
                model.Add(sum(x[p, d, t] for p in existing_members) == required_count)

        # 5. ペア制約
        for d_idx in range(len(dates) - 1):
            d_curr = dates[d_idx]
            d_next = dates[d_idx + 1]

            for work_curr, work_next_required in pair_rules.items():
                if work_curr in all_tasks and work_next_required in all_tasks:
                    for p in existing_members:
                        model.Add(x[p, d_next, work_next_required] == 1).OnlyEnforceIf(x[p, d_curr, work_curr])

        # -------------------------------------------------------------
        # 目的関数: エリア不一致コストの最小化 & 不要な変更の抑制
        # -------------------------------------------------------------
        objective_terms = []
        for p in existing_members:
            p_base_area = member_base_area.get(p, '')
            for d in dates:
                orig_t = initial_assignment.get((p, d), 'OFF')
                for t in all_tasks:
                    t_area = get_task_area(t)
                    
                    # ① エリア不一致に対する強いペナルティ (+1000)
                    if p_base_area and t_area and t_area != 'ANY' and p_base_area != t_area:
                        objective_terms.append(x[p, d, t] * 1000)
                    
                    # ② 不要なシフト変更への軽微なペナルティ (+1)
                    # (エリア解消のトレードなら -1000 の効果があるので余裕でトレードされる)
                    if t != orig_t:
                        objective_terms.append(x[p, d, t] * 1)

        model.Minimize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60.0
        status = solver.Solve(model)

        change_logs = []
        pair_applied_logs = []

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
                            break

            result_rows = []
            for p in existing_members:
                row = {
                    id_col_name: p,
                    name_col_name: member_names.get(p, '')
                }
                for d in dates:
                    row[d] = final_schedule.get((p, d), 'OFF')
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
                            f"【ペア制約適用】{p_name}さん({p}): {work_curr} ({d_curr}) ➔ 翌日必ず {work_next} ({d_next})"
                        )
            
            return df_result, True, "OK", change_logs, pair_applied_logs, [], []
        else:
            return df_initial_raw, False, f"Solver Status: {solver.StatusName(status)}", [], [], [], []

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("計算中..."):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg, change_logs, pair_debug_logs, _, _ = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("最適化計算が完了しました！")
                    if change_logs:
                        st.subheader("📋 変更（トレード）された勤務一覧")
                        for clog in change_logs:
                            st.write(clog)
                    else:
                        st.info("ℹ️ 初期シフトから変更の必要はありませんでした。")

                    with st.expander("🔍 適用されたペア制約ログ"):
                        for p_log in sorted(list(set(pair_debug_logs))):
                            st.write(p_log)

                    st.subheader("📊 最適化結果プレビュー")
                    st.dataframe(result_df)

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