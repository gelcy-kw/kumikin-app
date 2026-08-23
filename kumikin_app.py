import streamlit as st
import pandas as pd
from ortools.sat.python import cp_model

st.set_page_config(page_title="Task Schedule Optimizer", layout="centered")

def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 Restricted Access")
        pwd = st.text_input("Passcode", type="password")
        if st.button("Login"):
            if pwd == "1026":
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("Invalid Passcode")
        return False
    return True

if check_password():
    st.title("Task Schedule Optimizer")
    st.caption("Resource Allocation & Constraint Solver (DayType Supported)")

    st.subheader("1. Load Data Files")
    file_members = st.file_uploader("Member_Master.csv", type=["csv"])
    file_tasks = st.file_uploader("Task_Master.csv", type=["csv"])
    file_initial = st.file_uploader("Initial_Schedule.csv", type=["csv"])

    def run_optimization(df_members, df_tasks, df_initial_raw):
        # 1. 初期スケジュールの読み込み（1行目の DayType を取得）
        # 列構造: [Date/MemberID, 10/01, 10/02, ...]
        day_types_row = df_initial_raw[df_initial_raw.iloc[:, 0] == 'DayType']
        if day_types_row.empty:
            st.error("Initial_Schedule.csv の1行目に DayType 行が見つかりません。")
            return df_initial_raw, False
        
        # 日付ごとの DayType マッピング
        dates = list(df_initial_raw.columns[1:])
        day_type_map = {}
        for col in dates:
            day_type_map[str(col)] = str(day_types_row[col].values[0]).strip()

        # メンバー行のみ抽出
        df_members_sched = df_initial_raw[df_initial_raw.iloc[:, 0] != 'DayType'].copy()
        
        # 縦書き(行:日付, 列:人員)に変換
        df_initial_indexed = df_members_sched.set_index(df_members_sched.columns[0])
        df_initial_shift = df_initial_indexed.T.reset_index()
        df_initial_shift.rename(columns={'index': 'Date'}, inplace=True)

        model = cp_model.CpModel()
        
        members = df_members['MemberID'].astype(str).tolist()
        days = df_initial_shift['Date'].astype(str).tolist()
        
        # 2. タスクマスターの準備
        df_tasks['TaskID'] = df_tasks['TaskID'].astype(str)
        tasks_master = df_tasks.set_index('TaskID').to_dict('index')
        member_home = df_members.set_index(df_members['MemberID'].astype(str))['BaseArea'].to_dict()
        all_tasks = list(tasks_master.keys())

        # 表示用ID(101_W -> 101)から内部ID(101_W)へのマッピングテーブル構築
        # (表示名, DayType) -> 内部TaskID
        disp_to_internal = {}
        internal_to_disp = {}
        for t_id, t_info in tasks_master.items():
            disp_no = t_id.split('_')[0] if '_' in t_id else t_id
            d_type = str(t_info.get('DayType', 'All')).strip()
            disp_to_internal[(disp_no, d_type)] = t_id
            disp_to_internal[(disp_no, 'All')] = t_id
            internal_to_disp[t_id] = disp_no

        # 決定変数: x[p, d, t]
        x = {}
        for p in members:
            for d in days:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')
                    
        # ハード制約1: 1人1日1タスク
        for p in members:
            for d in days:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)
                
        # ハード制約2: 日別タスク割り当て数の維持 ＆ 固定日(Fixed)の適用
        for d_idx, d in enumerate(days):
            d_type = day_type_map.get(d, 'Weekday')
            raw_tasks = [str(val).strip() for val in df_initial_shift.iloc[d_idx].drop('Date').tolist()]
            
            # 各メンバーの初期タスクを内部IDに補正
            converted_day_tasks = []
            for p_idx, p in enumerate(members):
                raw_t = raw_tasks[p_idx]
                # 内部IDを取得 (無ければそのまま)
                internal_t = disp_to_internal.get((raw_t, d_type), disp_to_internal.get((raw_t, 'All'), raw_t))
                converted_day_tasks.append(internal_t)
                
                # もし Fixed (トレード対象外) の日なら、初期配置のまま固定！
                if d_type == 'Fixed':
                    if internal_t in all_tasks:
                        model.Add(x[p, d, internal_t] == 1)

            # タスク数の維持
            for t in all_tasks:
                count = converted_day_tasks.count(t)
                model.Add(sum(x[p, d, t] for p in members) == count)

        # ハード制約3: 連続ペアタスク制約
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            for t_id, t_info in tasks_master.items():
                pair_id = str(t_info.get('PairTaskID', ''))
                if pair_id and pair_id != 'nan' and pair_id in tasks_master:
                    for p in members:
                        model.Add(x[p, d_curr, t_id] == x[p, d_next, pair_id])

        # 目的関数（ペナルティ項の最小化）
        penalty_terms = []
        
        # 優先順位 1: 拠点ミスマッチペナルティ [重み: 1,000,000]
        for p in members:
            home_st = member_home[p]
            for d in days:
                for t_id, t_info in tasks_master.items():
                    if str(t_info['TargetArea']) != str(home_st):
                        penalty_terms.append(x[p, d, t_id] * 1000000)

        # 優先順位 2: Late-Early（おそはや）回避ペナルティ [重み: 1,000]
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            for p in members:
                for t1_id, t1_info in tasks_master.items():
                    if str(t1_info.get('EndType')) == 'Late':
                        for t2_id, t2_info in tasks_master.items():
                            if str(t2_info.get('StartType')) == 'Early':
                                late_early = model.NewBoolVar(f'le_{p}_{d_curr}_{t1_id}_{t2_id}')
                                model.AddBoolAnd([x[p, d_curr, t1_id], x[p, d_next, t2_id]]).OnlyEnforceIf(late_early)
                                model.AddBoolOr([x[p, d_curr, t1_id].Not(), x[p, d_next, t2_id].Not()]).OnlyEnforceIf(late_early.Not())
                                penalty_terms.append(late_early * 1000)

        # 優先順位 3: 負荷平準化ペナルティ [重み: 1]
        for p in members:
            p_diff = sum(x[p, d, t] * int(tasks_master[t]['Load']) for d in days for t in all_tasks)
            penalty_terms.append(p_diff)

        model.Minimize(sum(penalty_terms))
        
        # ソルバーの実行
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 30.0
        status = solver.Solve(model)
        
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            result_rows = []
            
            # DayType行を出力用に復元
            day_type_output_row = {'Date': 'DayType'}
            for d in days:
                day_type_output_row[d] = day_type_map.get(d, '')
            
            for d in days:
                row = {'Date': d}
                for p in members:
                    for t in all_tasks:
                        if solver.Value(x[p, d, t]) == 1:
                            # 出力時は内部ID (101_W) を 表示用ID (101) に変換して戻す！
                            row[p] = internal_to_disp.get(t, t)
                            break
                result_rows.append(row)
            
            # 横書きフォーマットに再転置して出力
            df_result_vert = pd.DataFrame(result_rows)
            df_result_horiz = df_result_vert.set_index('Date').T.reset_index()
            df_result_horiz.rename(columns={'index': 'Date'}, inplace=True)
            
            # 先頭に DayType 行を再挿入
            df_dt_row = pd.DataFrame([day_type_output_row])
            df_result_final = pd.concat([df_dt_row, df_result_horiz], ignore_index=True)
            df_result_final.rename(columns={'Date': df_initial_raw.columns[0]}, inplace=True)
            
            return df_result_final, True
        else:
            return df_initial_raw, False

    st.subheader("2. Run Solver")
    if st.button("Process Optimization"):
        if file_members and file_tasks and file_initial:
            with st.spinner("Solving constraint logic..."):
                df_m = pd.read_csv(file_members)
                df_t = pd.read_csv(file_tasks)
                df_i = pd.read_csv(file_initial)
                
                result_df, success = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("Optimization Completed.")
                else:
                    st.warning("No optimal solution found. Original schedule retained.")
                
                csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                st.download_button(
                    label="Download Optimized_Schedule.csv",
                    data=csv_data,
                    file_name="Optimized_Schedule.csv",
                    mime="text/csv"
                )
        else:
            st.error("Error: Please upload all 3 CSV files.")