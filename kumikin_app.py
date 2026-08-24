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

def normalize_task_id(t_str):
    """
    15M, M_15, M-15, 15_M などの表記揺れをすべて 'M_15' の標準形式に変換する関数
    """
    s = clean_str(t_str).upper()
    if not s or s == 'OFF':
        return 'OFF'
    
    # 数字+文字 (例: 15M, 46C) -> M_15, C_46
    m1 = re.match(r'^(\d+)([MC])$', s)
    if m1:
        num, role = m1.groups()
        return f"{role}_{num}"
    
    # 文字+数字 (例: M15, C46) -> M_15, C_46
    m2 = re.match(r'^([MC])(\d+)$', s)
    if m2:
        role, num = m2.groups()
        return f"{role}_{num}"

    # 文字+記号+数字 (例: M_15, M-15) -> M_15
    m3 = re.match(r'^([MC])[\-_](\d+)$', s)
    if m3:
        role, num = m3.groups()
        return f"{role}_{num}"

    # 数字+記号+文字 (例: 15_M, 15-M) -> M_15
    m4 = re.match(r'^(\d+)[\-_]([MC])$', s)
    if m4:
        num, role = m4.groups()
        return f"{role}_{num}"

    return s

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
        # Task_Masterの読み込みとID正規化辞書の構築
        # -------------------------------------------------------------
        # TargetArea列名の揺れ対策
        target_col = 'TargetArea'
        if 'TargetArea' not in df_tasks.columns:
            for col in df_tasks.columns:
                if 'target' in col.lower() or '拠点' in col or 'area' in col.lower():
                    target_col = col
                    break

        tasks_master = {}
        raw_to_norm = {}  # 元の表記から正規化IDへのマップ

        for _, row in df_tasks.iterrows():
            raw_id = clean_str(row['TaskID'])
            norm_id = normalize_task_id(raw_id)
            
            info = row.to_dict()
            info['TargetArea'] = clean_str(row.get(target_col, ''))
            
            # 正規化IDを主キーとして登録
            tasks_master[norm_id] = info
            raw_to_norm[raw_id] = norm_id

        all_tasks = list(tasks_master.keys())

        def get_task_info(t_id):
            norm = normalize_task_id(t_id)
            return tasks_master.get(norm, {})

        def get_disp_name(norm_t_id, original_raw=""):
            """表示用ID（例: 15M）に復元する関数"""
            if original_raw:
                return original_raw
            if norm_t_id == 'OFF':
                return 'OFF'
            m = re.match(r'^([MC])_(\d+)$', norm_t_id)
            if m:
                role, num = m.groups()
                return f"{num}{role}"
            return norm_t_id

        SPECIAL_DUTIES = (
            [f"A{i}" for i in range(1, 8)] +
            [f"J{i}" for i in range(1, 7)] +
            [f"R{i}" for i in range(1, 7)] +
            [f"S{i}" for i in range(1, 4)]
        )

        initial_assignment = {}
        day_converted_tasks = {d: [] for d in days}

        for d in days:
            day_row = df_initial_shift[df_initial_shift['Date'] == d]
            
            for p in existing_members:
                if not day_row.empty and p in day_row.columns:
                    raw_t = clean_str(day_row[p].values[0])
                else:
                    raw_t = 'OFF'
                
                norm_t = normalize_task_id(raw_t)
                
                # Masterに登録がない場合のみ新規作成
                if norm_t not in tasks_master and norm_t != 'OFF':
                    tasks_master[norm_t] = {'TargetArea': '', 'FemaleAllowed': 'Y', 'Role': 'All', 'Load': 0}
                    if norm_t not in all_tasks:
                        all_tasks.append(norm_t)

                if norm_t not in all_tasks and norm_t in tasks_master:
                    all_tasks.append(norm_t)

                initial_assignment[(p, d)] = (raw_t, norm_t)
                day_converted_tasks[d].append(norm_t)

        x = {}
        for p in existing_members:
            for d in days:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

        # 基本制約
        for d in days:
            d_type = day_type_map.get(d, 'Weekday')
            
            for p in existing_members:
                raw_t, norm_t = initial_assignment[(p, d)]

                if raw_t == 'OFF' or norm_t == 'OFF':
                    if 'OFF' in all_tasks:
                        model.Add(x[p, d, 'OFF'] == 1)
                elif raw_t in SPECIAL_DUTIES or norm_t in SPECIAL_DUTIES or d_type == 'Fixed':
                    model.Add(x[p, d, norm_t] == 1)

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
                    
                    disp_t = get_disp_name(t_id)
                    t_role = 'All'
                    if disp_t.endswith('M') or t_id.startswith('M_'):
                        t_role = 'M'
                    elif disp_t.endswith('C') or t_id.startswith('C_'):
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
            
            active_tasks_today = set(day_converted_tasks[d_curr])
            
            for t_id in active_tasks_today:
                if t_id == 'OFF':
                    continue
                t_info = get_task_info(t_id)
                pair_raw = clean_str(t_info.get('PairTaskID', ''))
                
                if pair_raw and pair_raw not in ['nan', 'None', '']:
                    norm_pair_id = normalize_task_id(pair_raw)
                    
                    if norm_pair_id in all_tasks:
                        pair_debug_logs.append(f"【ペア制約適用】{get_disp_name(t_id)} ({d_curr}) ➔ {get_disp_name(norm_pair_id, pair_raw)} ({d_next})")
                        for p in existing_members:
                            model.Add(x[p, d_curr, t_id] == x[p, d_next, norm_pair_id])

        # -------------------------------------------------------------
        # 目的関数 ＆ デバッグデータ収集
        # -------------------------------------------------------------
        penalty_terms = []
        score_debug_logs = []

        for p in existing_members:
            home_st = clean_str(members_info[p].get('BaseArea', '')).strip()
            for d in days:
                raw_t, orig_norm = initial_assignment[(p, d)]
                if orig_norm == 'OFF':
                    continue
                
                t_info = get_task_info(orig_norm)
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
                _, orig_norm = initial_assignment[(p, d)]
                if orig_norm in all_tasks:
                    penalty_terms.append((1 - x[p, d, orig_norm]) * 1)

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
                            assigned_disp = get_disp_name(t)
                            row[p] = assigned_disp
                            
                            orig_raw, orig_norm = initial_assignment[(p, d)]
                            if t != orig_norm and orig_norm != 'OFF':
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