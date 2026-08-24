import streamlit as st
import pandas as pd
import re
from ortools.sat.python import cp_model

# ページ基本設定
st.set_page_config(page_title="勤務変更補助システム", layout="centered")

def check_password():
    """暗証番号によるアクセス制限"""
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 アクセス制限")
        pwd = st.text_input("パスコードを入力してください", type="password")
        if st.button("ログイン"):
            if pwd == "1026":  # パスコード
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("パスコードが正しくありません")
        return False
    return True

def load_csv_safely(uploaded_file):
    """UTF-8 と Shift-JIS(CP932) の両対応で CSV を自動読み込み"""
    try:
        return pd.read_csv(uploaded_file, encoding='utf-8')
    except (UnicodeDecodeError, pd.errors.ParserError):
        uploaded_file.seek(0)
        return pd.read_csv(uploaded_file, encoding='cp932')

def clean_str(val):
    if pd.isna(val):
        return ""
    return str(val).strip()

if check_password():
    st.title("勤務変更補助システム")
    st.caption("自動シフトトレード・制約最適化ソルバー")

    st.subheader("1. データファイルのアップロード")
    file_members = st.file_uploader("メンバーマスター (Member_Master.csv)", type=["csv"])
    file_tasks = st.file_uploader("仕業マスター (Task_Master.csv)", type=["csv"])
    file_initial = st.file_uploader("初期勤務表 (Initial_Schedule.csv)", type=["csv"])

    def run_optimization(df_members, df_tasks, df_initial_raw):
        header_col = df_initial_raw.columns[0]
        
        # DayType行の取得
        day_types_row = df_initial_raw[df_initial_raw[header_col].astype(str).str.strip() == 'DayType']
        if day_types_row.empty:
            st.error("エラー: Initial_Schedule.csv に 'DayType' 行が見つかりません。")
            return df_initial_raw, False, "DayType行なし"
        
        dates = [clean_str(c) for c in df_initial_raw.columns[1:]]
        day_type_map = {}
        for col in df_initial_raw.columns[1:]:
            col_str = clean_str(col)
            day_type_map[col_str] = clean_str(day_types_row[col].values[0])

        # Member_Master のメンバー情報＆氏名取得
        df_members['MemberID'] = df_members['MemberID'].apply(clean_str)
        members_info = df_members.set_index('MemberID').to_dict('index')
        members = list(members_info.keys())

        # メンバーの行のみ抽出
        df_members_sched = df_initial_raw[df_initial_raw[header_col].apply(clean_str) != 'DayType'].copy()
        df_members_sched[header_col] = df_members_sched[header_col].apply(clean_str)

        df_initial_indexed = df_members_sched.set_index(header_col)
        df_initial_indexed.columns = [clean_str(c) for c in df_initial_indexed.columns]
        
        existing_members = [m for m in members if m in df_initial_indexed.index]
        if len(existing_members) == 0:
            st.error("エラー: Initial_Schedule.csv のメンバーIDが Member_Master と一致しません。")
            return df_initial_raw, False, "メンバーID不一致"
            
        df_initial_indexed = df_initial_indexed.loc[existing_members]

        df_initial_shift = df_initial_indexed.T
        df_initial_shift.index = [clean_str(idx) for idx in df_initial_shift.index]
        df_initial_shift = df_initial_shift.reset_index().rename(columns={'index': 'Date'})

        model = cp_model.CpModel()
        days = dates
        
        # タスクマスターの準備
        df_tasks['TaskID'] = df_tasks['TaskID'].apply(clean_str)
        tasks_master = df_tasks.set_index('TaskID').to_dict('index')
        all_tasks = list(tasks_master.keys())

        # マッピング用辞書の構築
        disp_to_internal = {}
        internal_to_disp = {}
        
        for t_id, t_info in tasks_master.items():
            d_type = clean_str(t_info.get('DayType', 'All'))
            disp_to_internal[(t_id, d_type)] = t_id
            disp_to_internal[(t_id, 'All')] = t_id
            
            m = re.match(r'^([MC])_(\d+)$', t_id)
            if m:
                role, num = m.groups()
                alt_id = f"{num}{role}"
                disp_to_internal[(alt_id, d_type)] = t_id
                disp_to_internal[(alt_id, 'All')] = t_id
                internal_to_disp[t_id] = alt_id
            else:
                internal_to_disp[t_id] = t_id

        SPECIAL_DUTIES = (
            [f"A{i}" for i in range(1, 8)] +
            [f"J{i}" for i in range(1, 7)] +
            [f"R{i}" for i in range(1, 7)] +
            [f"S{i}" for i in range(1, 4)]
        )

        initial_assignment = {}
        day_converted_tasks = {d: [] for d in days}

        # 初期内部タスクを特定
        for d in days:
            d_type = day_type_map.get(d, 'Weekday')
            day_row = df_initial_shift[df_initial_shift['Date'] == d]
            
            for p in existing_members:
                if not day_row.empty and p in day_row.columns:
                    raw_t = clean_str(day_row[p].values[0])
                else:
                    raw_t = 'OFF'
                
                internal_t = disp_to_internal.get((raw_t, d_type), disp_to_internal.get((raw_t, 'All'), raw_t))
                
                if internal_t not in all_tasks:
                    all_tasks.append(internal_t)
                    tasks_master[internal_t] = {'TargetArea': '', 'FemaleAllowed': 'Y', 'Role': 'All', 'Load': 0}
                    internal_to_disp[internal_t] = raw_t

                initial_assignment[(p, d)] = (raw_t, internal_t)
                day_converted_tasks[d].append(internal_t)

        # 決定変数 x[p, d, t] の定義
        x = {}
        for p in existing_members:
            for d in days:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

        # 制約の適用
        for d in days:
            d_type = day_type_map.get(d, 'Weekday')
            
            for p in existing_members:
                raw_t, internal_t = initial_assignment[(p, d)]

                # 1. OFF・特殊仕業・固定日の固定
                if raw_t == 'OFF' or internal_t == 'OFF':
                    if 'OFF' in all_tasks:
                        model.Add(x[p, d, 'OFF'] == 1)
                elif raw_t in SPECIAL_DUTIES or internal_t in SPECIAL_DUTIES or d_type == 'Fixed':
                    model.Add(x[p, d, internal_t] == 1)

            # 1人1日1タスク（絶対遵守）
            for p in existing_members:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

            # 日ごとの各タスク総数を維持（全体枠の保存）
            tasks_today = day_converted_tasks[d]
            for t in set(tasks_today):
                count = tasks_today.count(t)
                model.Add(sum(x[p, d, t] for p in existing_members) == count)

        # 性別・役割不適合のトレード禁止（ハード制約）
        for p in existing_members:
            p_gender = clean_str(members_info[p].get('Gender', 'M'))
            p_role = clean_str(members_info[p].get('Role', 'MC'))
            
            for d in days:
                for t_id in all_tasks:
                    t_info = tasks_master.get(t_id, {})
                    t_female_allowed = clean_str(t_info.get('FemaleAllowed', 'Y'))
                    t_role = clean_str(t_info.get('Role', 'All'))
                    
                    if p_gender == 'F' and t_female_allowed == 'N':
                        model.Add(x[p, d, t_id] == 0)
                        
                    if (p_role == 'M' and t_role == 'C') or (p_role == 'C' and t_role == 'M'):
                        model.Add(x[p, d, t_id] == 0)

        # -------------------------------------------------------------
        # 目的関数: トレード最適化の評価
        # -------------------------------------------------------------
        penalty_terms = []

        # 1. 拠点ミスマッチペナルティ [重み: 1,000,000点]
        for p in existing_members:
            home_st = clean_str(members_info[p].get('BaseArea', ''))
            for d in days:
                for t_id in all_tasks:
                    if t_id == 'OFF':
                        continue
                    t_info = tasks_master.get(t_id, {})
                    target_area = clean_str(t_info.get('TargetArea', ''))
                    if target_area and home_st and target_area != home_st:
                        penalty_terms.append(x[p, d, t_id] * 1000000)

        # 2. 連続ペアタスク違反 [重み: 100,000点]
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            next_d_type = day_type_map.get(d_next, 'Weekday')
            
            for t_id, t_info in tasks_master.items():
                pair_raw = clean_str(t_info.get('PairTaskID', ''))
                if pair_raw and pair_raw not in ['nan', 'None', '']:
                    resolved_pair_id = disp_to_internal.get(
                        (pair_raw, next_d_type), 
                        disp_to_internal.get((pair_raw, 'All'), pair_raw)
                    )
                    
                    if resolved_pair_id in all_tasks:
                        for p in existing_members:
                            pair_violation = model.NewBoolVar(f'pv_{p}_{d_curr}_{t_id}')
                            model.AddBoolAnd([x[p, d_curr, t_id], x[p, d_next, resolved_pair_id].Not()]).OnlyEnforceIf(pair_violation)
                            model.AddBoolOr([x[p, d_curr, t_id].Not(), x[p, d_next, resolved_pair_id]]).OnlyEnforceIf(pair_violation.Not())
                            penalty_terms.append(pair_violation * 100000)

        # 3. Late-Early (遅番→早番) 回避 [重み: 1,000点]
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            for p in existing_members:
                for t1_id, t1_info in tasks_master.items():
                    if clean_str(t1_info.get('EndType')) == 'Late':
                        for t2_id, t2_info in tasks_master.items():
                            if clean_str(t2_info.get('StartType')) == 'Early':
                                late_early = model.NewBoolVar(f'le_{p}_{d_curr}_{t1_id}_{t2_id}')
                                model.AddBoolAnd([x[p, d_curr, t1_id], x[p, d_next, t2_id]]).OnlyEnforceIf(late_early)
                                model.AddBoolOr([x[p, d_curr, t1_id].Not(), x[p, d_next, t2_id].Not()]).OnlyEnforceIf(late_early.Not())
                                penalty_terms.append(late_early * 1000)

        # 4. トレード微小コスト（無意味な変更の抑制: 1点）
        for p in existing_members:
            for d in days:
                raw_t, init_t = initial_assignment[(p, d)]
                if (p, d, init_t) in x:
                    is_changed = model.NewBoolVar(f'chg_{p}_{d}')
                    model.Add(x[p, d, init_t] == 0).OnlyEnforceIf(is_changed)
                    model.Add(x[p, d, init_t] == 1).OnlyEnforceIf(is_changed.Not())
                    penalty_terms.append(is_changed * 1)

        if penalty_terms:
            model.Minimize(sum(penalty_terms))
        
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 30.0
        status = solver.Solve(model)
        
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            result_rows = []
            for d in days:
                row = {'Date': d}
                for p in existing_members:
                    for t in all_tasks:
                        if (p, d, t) in x and solver.Value(x[p, d, t]) == 1:
                            row[p] = internal_to_disp.get(t, t)
                            break
                result_rows.append(row)
            
            df_result_vert = pd.DataFrame(result_rows)
            df_result_horiz = df_result_vert.set_index('Date').T.reset_index()
            df_result_horiz.rename(columns={'index': header_col}, inplace=True)
            
            # Name列の挿入
            names_list = []
            for pid in df_result_horiz[header_col]:
                pid_str = clean_str(pid)
                name_val = clean_str(members_info.get(pid_str, {}).get('Name', ''))
                names_list.append(name_val)
            
            df_result_horiz.insert(1, 'Name', names_list)

            day_type_output_row = {header_col: 'DayType', 'Name': ''}
            for d in days:
                day_type_output_row[d] = day_type_map.get(d, '')
            
            df_dt_row = pd.DataFrame([day_type_output_row])
            df_result_final = pd.concat([df_dt_row, df_result_horiz], ignore_index=True)
            
            return df_result_final, True, "OK"
        else:
            return df_initial_raw, False, f"Solver Status: {solver.StatusName(status)}"

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("安全なトレード条件で最適化計算中..."):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("最適化計算が正常に完了しました！")
                    csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="調整済みシフト表(Optimized_Schedule.csv)をダウンロード",
                        data=csv_data,
                        file_name="Optimized_Schedule.csv",
                        mime="text/csv"
                    )
                else:
                    st.error(f"解が見つかりませんでした。（詳細: {log_msg}）")
        else:
            st.error("エラー: 3つのファイルをすべてアップロードしてください。")