import streamlit as st
import pandas as pd
from ortools.sat.python import cp_model

# ページ基本設定
st.set_page_config(page_title="Task Schedule Optimizer", layout="centered")

def check_password():
    """暗証番号による簡易保護機能"""
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 Restricted Access")
        pwd = st.text_input("Passcode", type="password")
        if st.button("Login"):
            if pwd == "1026":  # ←お好みのパスワードに変更してください
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("Invalid Passcode")
        return False
    return True

if check_password():
    # --- メイン画面UI ---
    st.title("Task Schedule Optimizer")
    st.caption("Resource Allocation & Constraint Solver")

    st.subheader("1. Load Data Files")
    file_members = st.file_uploader("Member_Master.csv", type=["csv"])
    file_tasks = st.file_uploader("Task_Master.csv", type=["csv"])
    file_initial = st.file_uploader("Initial_Schedule.csv", type=["csv"])

    def run_optimization(df_members, df_tasks, df_initial_raw):
        # -------------------------------------------------------------
        # 横書きフォーマット(行:人員, 列:日付)を縦書き(行:日付, 列:人員)に内部転置
        # -------------------------------------------------------------
        df_initial_indexed = df_initial_raw.set_index(df_initial_raw.columns[0])
        df_initial_shift = df_initial_indexed.T.reset_index()
        df_initial_shift.rename(columns={'index': 'Date'}, inplace=True)

        model = cp_model.CpModel()
        
        members = df_members['MemberID'].astype(str).tolist()
        days = df_initial_shift['Date'].astype(str).tolist()
        
        df_tasks['TaskID'] = df_tasks['TaskID'].astype(str)
        tasks_master = df_tasks.set_index('TaskID').to_dict('index')
        member_home = df_members.set_index(df_members['MemberID'].astype(str))['BaseArea'].to_dict()
        all_tasks = list(tasks_master.keys())
        
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
                
        # ハード制約2: 日別タスク割り当て数の維持
        for d_idx, d in enumerate(days):
            initial_day_tasks = [str(val) for val in df_initial_shift.iloc[d_idx].drop('Date').tolist()]
            for t in all_tasks:
                count = initial_day_tasks.count(t)
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
                                late_early = model.NewBoolVar(f'le_{p}_{d_curr}')
                                model.AddBoolAnd([x[p, d_curr, t1_id], x[p, d_next, t2_id]]).Eavesdrop(late_early)
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
            for d in days:
                row = {'Date': d}
                for p in members:
                    for t in all_tasks:
                        if solver.Value(x[p, d, t]) == 1:
                            row[p] = t
                            break
                result_rows.append(row)
            
            # 横書きフォーマット（行:人員, 列:日付）に再転置して出力
            df_result_vert = pd.DataFrame(result_rows)
            df_result_horiz = df_result_vert.set_index('Date').T.reset_index()
            df_result_horiz.rename(columns={'index': 'Date'}, inplace=True)
            
            return df_result_horiz, True
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