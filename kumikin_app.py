import streamlit as st
import pandas as pd
import re
from ortools.sat.python import cp_model

# ページ基本設定
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
    return str(val).strip()

if check_password():
    st.title("勤務変更補助システム")
    st.caption("自動シフトトレード・エリア最適化ソルバー")

    st.subheader("1. データファイルのアップロード")
    file_members = st.file_uploader("メンバーマスター (Member_Master.csv)", type=["csv"])
    file_tasks = st.file_uploader("仕業マスター (Task_Master.csv)", type=["csv"])
    file_initial = st.file_uploader("初期勤務表 (Initial_Schedule.csv)", type=["csv"])

    def run_optimization(df_members, df_tasks, df_initial_raw):
        header_col = df_initial_raw.columns[0]
        dates = [clean_str(c) for c in df_initial_raw.columns[1:]]

        # -------------------------------------------------------------
        # 1. メンバーマスターのパース (MemberID ➔ BaseArea)
        # -------------------------------------------------------------
        df_members['MemberID'] = df_members['MemberID'].apply(clean_str)
        member_base_area = {}
        for _, row in df_members.iterrows():
            m_id = clean_str(row['MemberID'])
            area = clean_str(row.get('BaseArea', ''))
            member_base_area[m_id] = area

        members = list(member_base_area.keys())

        # -------------------------------------------------------------
        # 2. 仕業マスターのパース (数字と種別 M/C から TargetArea を特定)
        # -------------------------------------------------------------
        # 例: ("15", "M") -> "Akaike", ("60", "M") -> "Joshin"
        task_area_map = {}
        if 'TaskID' in df_tasks.columns and 'TargetArea' in df_tasks.columns:
            for _, row in df_tasks.iterrows():
                t_id = clean_str(row['TaskID'])
                t_area = clean_str(row['TargetArea'])
                
                # M_15_W や C_60_W から プレフィックス(M/C) と 数字(15/60) を抽出
                m = re.match(r'([MC])_(\d+)', t_id)
                if m:
                    prefix = m.group(1)
                    num = m.group(2)
                    task_area_map[(num, prefix)] = t_area

        def get_task_area(task_code):
            if not task_code or task_code == 'OFF':
                return 'ANY'
            # 15M -> num="15", prefix="M"
            m = re.match(r'(\d+)([MC])', task_code)
            if m:
                num = m.group(1)
                prefix = m.group(2)
                return task_area_map.get((num, prefix), 'ANY')
            # C15 などのパターン
            m = re.match(r'([MC])(\d+)', task_code)
            if m:
                prefix = m.group(1)
                num = m.group(2)
                return task_area_map.get((num, prefix), 'ANY')
            return 'ANY'

        # -------------------------------------------------------------
        # 3. 初期勤務表データの整理
        # -------------------------------------------------------------
        df_members_sched = df_initial_raw[df_initial_raw[header_col].apply(clean_str) != 'DayType'].copy()
        df_members_sched[header_col] = df_members_sched[header_col].apply(clean_str)

        df_initial_indexed = df_members_sched.set_index(header_col)
        df_initial_indexed.columns = [clean_str(c) for c in df_initial_indexed.columns]
        
        existing_members = [m for m in members if m in df_initial_indexed.index]
        if len(existing_members) == 0:
            return df_initial_raw, False, "メンバーID不一致", [], [], [], []

        initial_assignment = {}
        all_tasks_set = set(['OFF'])

        for p in existing_members:
            for d in dates:
                val = clean_str(df_initial_indexed.loc[p, d])
                initial_assignment[(p, d)] = val
                if val:
                    all_tasks_set.add(val)

        all_tasks = list(all_tasks_set)

        # ペア制約ルールの定義（1日目 ➔ 2日目）
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

        # 制約1: 1人1日1仕業
        for d in dates:
            for p in existing_members:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

        # 制約2: 各日に必要な仕業の人数（需要数）を維持
        for d in dates:
            tasks_today = [initial_assignment.get((p, d), 'OFF') for p in existing_members]
            for t in all_tasks:
                required_count = tasks_today.count(t)
                model.Add(sum(x[p, d, t] for p in existing_members) == required_count)

        # 制約3: ペア制約（誰であれ、当日work_currを担当したら翌日はwork_next_requiredになる）
        pair_debug_logs = []
        for d_idx in range(len(dates) - 1):
            d_curr = dates[d_idx]
            d_next = dates[d_idx + 1]

            for work_curr, work_next_required in pair_rules.items():
                if work_curr in all_tasks and work_next_required in all_tasks:
                    for p in existing_members:
                        # pさんが当日work_currなら翌日work_next_required
                        model.Add(x[p, d_next, work_next_required] == 1).OnlyEnforceIf(x[p, d_curr, work_curr])

        # 初期シフトで適用されていたペア制約のログ記録
        for d_idx in range(len(dates) - 1):
            d_curr = dates[d_idx]
            d_next = dates[d_idx + 1]
            for p in existing_members:
                work_curr = initial_assignment.get((p, d_curr), "")
                if work_curr in pair_rules:
                    work_next_required = pair_rules[work_curr]
                    pair_debug_logs.append(
                        f"【ペア制約適用】{p}さん: {work_curr} ({d_curr}) ➔ 翌日必ず {work_next_required} ({d_next})"
                    )

        # -------------------------------------------------------------
        # 4. 目的関数: エリア不一致コストの最小化
        # -------------------------------------------------------------
        objective_terms = []
        
        for p in existing_members:
            p_base_area = member_base_area.get(p, '')
            
            for d in dates:
                orig_t = initial_assignment.get((p, d), 'OFF')
                
                for t in all_tasks:
                    t_area = get_task_area(t)
                    
                    # エリア不一致ペナルティ（所属と仕業エリアが異なる場合）
                    if p_base_area and t_area and t_area != 'ANY' and p_base_area != t_area:
                        objective_terms.append(x[p, d, t] * 1000)
                    
                    # 初期シフト維持ボーナス（無駄な変更を防ぐ）
                    if t == orig_t:
                        objective_terms.append(x[p, d, t] * -10)

        model.Minimize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60.0
        status = solver.Solve(model)

        change_logs = []
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            result_rows = []
            for d in dates:
                row = {'Date': d}
                for p in existing_members:
                    for t in all_tasks:
                        if solver.Value(x[p, d, t]) == 1:
                            row[p] = t
                            orig_t = initial_assignment.get((p, d), 'OFF')
                            if t != orig_t:
                                change_logs.append(f"【{d}】{p}さん : {orig_t} ➔ {t}")
                            break
                result_rows.append(row)
            
            df_result_vert = pd.DataFrame(result_rows)
            df_result_horiz = df_result_vert.set_index('Date').T.reset_index()
            df_result_horiz.rename(columns={'index': header_col}, inplace=True)
            
            return df_result_horiz, True, "OK", change_logs, pair_debug_logs, [], []
        else:
            return df_initial_raw, False, f"Solver Status: {solver.StatusName(status)}", [], pair_debug_logs, [], []

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

                    csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="Optimized_Schedule.csv をダウンロード",
                        data=csv_data,
                        file_name="Optimized_Schedule.csv",
                        mime="text/csv"
                    )
                else:
                    st.error(f"解が見つかりませんでした。（詳細: {log_msg}）")
                    with st.expander("🔍 デバッグログ（ペア制約の適用状況）"):
                        for p_log in sorted(list(set(pair_debug_logs))):
                            st.write(p_log)
        else:
            st.error("エラー: 3つのファイルをすべてアップロードしてください。")