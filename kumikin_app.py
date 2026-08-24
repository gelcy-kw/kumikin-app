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
        
        day_types_row = df_initial_raw[df_initial_raw[header_col].astype(str).str.strip() == 'DayType']
        if day_types_row.empty:
            st.error("エラー: Initial_Schedule.csv に 'DayType' 行が見つかりません。")
            return df_initial_raw, False, "DayType行なし", [], [], []
        
        dates = [clean_str(c) for c in df_initial_raw.columns[1:]]
        day_type_map = {}
        for col in df_initial_raw.columns[1:]:
            col_str = clean_str(col)
            day_type_map[col_str] = clean_str(day_types_row[col].values[0])

        df_members['MemberID'] = df_members['MemberID'].apply(clean_str)
        members_info = df_members.set_index('MemberID').to_dict('index')
        members = list(members_info.keys())

        df_members_sched = df_initial_raw[df_initial_raw[header_col].apply(clean_str) != 'DayType'].copy()
        df_members_sched[header_col] = df_members_sched[header_col].apply(clean_str)

        df_initial_indexed = df_members_sched.set_index(header_col)
        df_initial_indexed.columns = [clean_str(c) for c in df_initial_indexed.columns]
        
        existing_members = [m for m in members if m in df_initial_indexed.index]
        if len(existing_members) == 0:
            st.error("エラー: Initial_Schedule.csv のメンバーIDが Member_Master と一致しません。")
            return df_initial_raw, False, "メンバーID不一致", [], [], []
            
        df_initial_indexed = df_initial_indexed.loc[existing_members]

        df_initial_shift = df_initial_indexed.T
        df_initial_shift.index = [clean_str(idx) for idx in df_initial_shift.index]
        df_initial_shift = df_initial_shift.reset_index().rename(columns={'index': 'Date'})

        model = cp_model.CpModel()
        days = dates
        
        # -------------------------------------------------------------
        # Task_Masterの読み込みとエイリアス登録（完全相互参照化）
        # -------------------------------------------------------------
        df_tasks['TaskID'] = df_tasks['TaskID'].apply(clean_str)
        
        # TargetArea列名の揺れ対策
        target_col = 'TargetArea'
        if 'TargetArea' not in df_tasks.columns:
            for col in df_tasks.columns:
                if 'target' in col.lower() or '拠点' in col or 'area' in col.lower():
                    target_col = col
                    break

        tasks_master = {}
        disp_to_internal = {}
        internal_to_disp = {}

        for _, row in df_tasks.iterrows():
            t_id = clean_str(row['TaskID'])
            info = row.to_dict()
            info['TargetArea'] = clean_str(row.get(target_col, ''))
            
            # 元IDで登録
            tasks_master[t_id] = info
            
            # M_15 <-> 15M 相互変換エイリアスの生成
            d_type = clean_str(info.get('DayType', 'All'))
            disp_to_internal[(t_id, d_type)] = t_id
            disp_to_internal[(t_id, 'All')] = t_id
            
            m = re.match(r'^([MC])_(\d+)$', t_id)
            if m:
                role, num = m.groups()
                alt_id = f"{num}{role}"
                tasks_master[alt_id] = info  # エイリアスでも直接参照可能にする
                disp_to_internal[(alt_id, d_type)] = t_id
                disp_to_internal[(alt_id, 'All')] = t_id
                internal_to_disp[t_id] = alt_id
            else:
                m2 = re.match(r'^(\d+)([MC])$', t_id)
                if m2:
                    num, role = m2.groups()
                    alt_id = f"{role}_{num}"
                    tasks_master[alt_id] = info  # エイリアスでも直接参照可能にする
                    disp_to_internal[(alt_id, d_type)] = t_id
                    disp_to_internal[(alt_id, 'All')] = t_id
                internal_to_disp[t_id] = t_id

        all_tasks = list(tasks_master.keys())

        def get_task_info(t_id):
            return tasks_master.get(t_id, {})

        SPECIAL_DUTIES = (
            [f"A{i}" for i in range(1, 8)] +
            [f"J{i}" for i in range(1, 7)] +
            [f"R{i}" for i in range(1, 7)] +
            [f"S{i}" for i in range(1, 4)]
        )

        initial_assignment = {}
        day_converted_tasks = {d: [] for d in days}

        for d in days:
            d_type = day_type_map.get(d, 'Weekday')
            day_row = df_initial_shift[df_initial_shift['Date'] == d]
            
            for p in existing_members:
                if not day_row.empty and p in day_row.columns:
                    raw_t = clean_str(day_row[p].values[0])
                else:
                    raw_t = 'OFF'
                
                internal_t = disp_to_internal.get((raw_t, d_type), disp_to_internal.get((raw_t, 'All'), raw_t))
                
                if internal_t not in tasks_master:
                    tasks_master[internal_t] = {'TargetArea': '', 'FemaleAllowed': 'Y', 'Role': 'All', 'Load': 0}
                    internal_to_disp[internal_t] = raw_t
                if internal_t not in all_tasks:
                    all_tasks.append(internal_t)

                initial_assignment[(p, d)] = (raw_t, internal_t)
                day_converted_tasks[d].append(internal_t)

        x = {}
        for p in existing_members:
            for d in days:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

        # 基本制約
        for d in days:
            d_type = day_type_map.get(d, 'Weekday')
            
            for p in existing_members:
                raw_t, internal_t = initial_assignment[(p, d)]

                if raw_t == 'OFF' or internal_t == 'OFF':
                    if 'OFF' in all_tasks:
                        model.Add(x[p, d, 'OFF'] == 1)
                elif raw_t in SPECIAL_DUTIES or internal_t in SPECIAL_DUTIES or d_type == 'Fixed':
                    model.Add(x[p, d, internal_t] == 1)

            for p in existing_members:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

            tasks_today = day_converted_tasks[d]
            for t in set(tasks_today):
                count = tasks_today.count(t)
                model.Add(sum(x[p, d, t] for p in existing_members) == count)

        # 属性不適合の完全ガード
        for p in existing_members:
            p_gender = clean_str(members_info[p].get('Gender', 'M')).upper()
            p_role = clean_str(members_info[p].get('Role', 'MC')).upper()
            
            for d in days:
                for t_id in all_tasks:
                    t_info = get_task_info(t_id)
                    t_female_allowed = clean_str(t_info.get('FemaleAllowed', 'Y')).upper()
                    t_role = clean_str(t_info.get('Role', 'All')).upper()
                    
                    disp_t = internal_to_disp.get(t_id, t_id)
                    if disp_t.endswith('M'):
                        t_role = 'M'
                    elif disp_t.endswith('C'):
                        t_role = 'C'

                    if p_gender == 'F' and t_female_allowed == 'N':
                        model.Add(x[p, d, t_id] == 0)
                        
                    if (p_role == 'M' and t_role == 'C') or (p_role == 'C' and t_role == 'M'):
                        model.Add(x[p, d, t_id] == 0)

        # ペア仕業（一泊二日）のハード制約
        pair_debug_logs = []
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            next_d_type = day_type_map.get(d_next, 'Weekday')
            
            active_tasks_today = set(day_converted_tasks[d_curr])
            
            for t_id in active_tasks_today:
                if t_id == 'OFF':
                    continue
                t_info = get_task_info(t_id)
                pair_raw = clean_str(t_info.get('PairTaskID', ''))
                
                if pair_raw and pair_raw not in ['nan', 'None', '']:
                    resolved_pair_id = disp_to_internal.get(
                        (pair_raw, next_d_type), 
                        disp_to_internal.get((pair_raw, 'All'), 
                        disp_to_internal.get((f"M_{pair_raw}", next_d_type), pair_raw))
                    )
                    
                    if resolved_pair_id in all_tasks:
                        pair_debug_logs.append(f"【ペア制約適用】{internal_to_disp.get(t_id, t_id)} ({d_curr}) ➔ {internal_to_disp.get(resolved_pair_id, resolved_pair_id)} ({d_next})")
                        for p in existing_members:
                            model.Add(x[p, d_curr, t_id] == x[p, d_next, resolved_pair_id])

        # -------------------------------------------------------------
        # 目的関数 ＆ デバッグデータ収集
        # -------------------------------------------------------------
        penalty_terms = []
        score_debug_logs = []

        for p in existing_members:
            home_st = clean_str(members_info[p].get('BaseArea', '')).strip()
            for d in days:
                raw_t, orig_int = initial_assignment[(p, d)]
                if orig_int == 'OFF':
                    continue
                
                t_info = get_task_info(orig_int)
                target_area = clean_str(t_info.get('TargetArea', '')).strip()
                
                score_debug_logs.append(
                    f"【初期状態診断】{d} メンバー:{p}(所属:{home_st}) ➔ 担当仕業:{raw_t}(TargetArea:'{target_area}')"
                )

                for t_id in all_tasks:
                    if t_id == 'OFF':
                        continue
                    t_info_curr = get_task_info(t_id)
                    t_target = clean_str(t_info_curr.get('TargetArea', '')).strip()
                    
                    if home_st and t_target:
                        if t_target.lower() != home_st.lower():
                            penalty_terms.append(x[p, d, t_id] * 1000)
                        else:
                            penalty_terms.append(x[p, d, t_id] * (-100))

        for d in days:
            for p in existing_members:
                _, orig_int = initial_assignment[(p, d)]
                if orig_int in all_tasks:
                    penalty_terms.append((1 - x[p, d, orig_int]) * 1)

        if penalty_terms:
            model.Minimize(sum(penalty_terms))
        
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60.0
        status = solver.Solve(model)
        
        change_logs = []
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            result_rows = []
            for d in days:
                row = {'Date': d}
                for p in existing_members:
                    for t in all_tasks:
                        if (p, d, t) in x and solver.Value(x[p, d, t]) == 1:
                            assigned_disp = internal_to_disp.get(t, t)
                            row[p] = assigned_disp
                            
                            orig_raw, orig_int = initial_assignment[(p, d)]
                            if assigned_disp != orig_raw and orig_raw != 'OFF':
                                change_logs.append(f"【{d}】{p} : {orig_raw} ➔ {assigned_disp}")
                            break
                result_rows.append(row)
            
            df_result_vert = pd.DataFrame(result_rows)
            df_result_horiz = df_result_vert.set_index('Date').T.reset_index()
            df_result_horiz.rename(columns={'index': header_col}, inplace=True)
            
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
            
            return df_result_final, True, "OK", change_logs, pair_debug_logs, score_debug_logs
        else:
            return df_initial_raw, False, f"Solver Status: {solver.StatusName(status)}", [], pair_debug_logs, score_debug_logs

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("トレード判定中..."):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg, change_logs, pair_debug_logs, score_debug_logs = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("最適化計算が正常に完了しました！")
                    
                    st.subheader("📋 変更された勤務一覧")
                    if change_logs:
                        for log in change_logs:
                            st.write(log)
                    else:
                        st.info("ℹ️ 条件を満たす効果的なトレードが存在しなかったため、無駄な変更を行わず初期シフトを維持しました。")

                    with st.expander("🔍 適用されたペア制約ログ"):
                        for p_log in list(set(pair_debug_logs)):
                            st.write(p_log)

                    with st.expander("🔍 初期シフトの拠点マッチング診断ログ"):
                        for s_log in score_debug_logs[:20]:
                            st.write(s_log)

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