import streamlit as st
import pandas as pd
from ortools.sat.python import cp_model

# ページ基本設定
st.set_page_config(page_title="勤務変更補助システム", layout="centered")

def check_password():
    """暗証番号によるアクセス制限"""
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 アクセス制限")
        pwd = st.text_input("パスコードを入力してください", type="password", key="pwd_input")
        if st.button("ログイン", key="login_btn"):
            if pwd == "1026":  # パスコード
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("パスコードが正しくありません")
        return False
    return True

def read_csv_safe(file_or_path):
    """Shift_JIS(CP932)やUTF-8などの文字コードの違いを自動判別して安全にCSVを読み込む"""
    encodings = ['utf-8-sig', 'utf-8', 'cp932', 'shift_jis']
    for enc in encodings:
        try:
            file_or_path.seek(0)
            return pd.read_csv(file_or_path, encoding=enc)
        except (UnicodeDecodeError, Exception):
            continue
    file_or_path.seek(0)
    return pd.read_csv(file_or_path, encoding='cp932', encoding_errors='replace')

def run_optimization(df_members, df_tasks, df_initial_raw):
    # 0. 空列・不要列の除去クリーニング
    df_initial_raw = df_initial_raw.loc[:, ~df_initial_raw.columns.str.contains('^Unnamed')]
    df_initial_raw = df_initial_raw.loc[:, df_initial_raw.columns.notna() & (df_initial_raw.columns != '')]

    header_col = df_initial_raw.columns[0]
    
    # DayType行の取得
    day_types_row = df_initial_raw[df_initial_raw[header_col].astype(str).str.strip().str.upper() == 'DAYTYPE']
    if day_types_row.empty:
        return df_initial_raw, False, "Initial_Schedule.csv に 'DayType' 行が見つかりません。"
    
    # 日付ごとの DayType マッピング
    dates = [str(c).strip() for c in df_initial_raw.columns[1:]]
    day_type_map = {}
    for col in df_initial_raw.columns[1:]:
        col_str = str(col).strip()
        day_type_map[col_str] = str(day_types_row[col].values[0]).strip()

    # Member_Master の準備
    df_members['MemberID'] = df_members['MemberID'].astype(str).str.strip()
    members = df_members['MemberID'].tolist()
    member_home = df_members.set_index('MemberID')['BaseArea'].astype(str).str.strip().to_dict()
    
    has_name_in_master = 'Name' in df_members.columns
    member_names = df_members.set_index('MemberID')['Name'].astype(str).str.strip().to_dict() if has_name_in_master else {}

    if 'Role' in df_members.columns:
        member_role = df_members.set_index('MemberID')['Role'].astype(str).str.strip().str.upper().to_dict()
    else:
        member_role = {m: 'MC' for m in members}

    if 'Gender' in df_members.columns:
        member_gender = df_members.set_index('MemberID')['Gender'].astype(str).str.strip().str.upper().to_dict()
    else:
        member_gender = {m: 'M' for m in members}

    df_members_sched = df_initial_raw[df_initial_raw[header_col].astype(str).str.strip().str.upper() != 'DAYTYPE'].copy()
    df_members_sched[header_col] = df_members_sched[header_col].astype(str).str.strip()

    df_initial_indexed = df_members_sched.set_index(header_col)
    df_initial_indexed.columns = [str(c).strip() for c in df_initial_indexed.columns]
    
    existing_members = [m for m in members if m in df_initial_indexed.index]
    if len(existing_members) == 0:
        return df_initial_raw, False, "Initial_Schedule.csv のメンバーIDが Member_Master と一致しません。"
        
    df_initial_indexed = df_initial_indexed.loc[existing_members]

    df_initial_shift = df_initial_indexed.T
    df_initial_shift.index = [str(idx).strip() for idx in df_initial_shift.index]
    df_initial_shift = df_initial_shift.reset_index().rename(columns={'index': 'Date'})

    model = cp_model.CpModel()
    days = dates
    
    # タスクマスターの準備
    df_tasks['TaskID'] = df_tasks['TaskID'].astype(str).str.strip().str.upper()
    tasks_master = df_tasks.set_index('TaskID').to_dict('index')
    all_tasks = list(tasks_master.keys())

    disp_to_internal = {}
    internal_to_disp = {}
    for t_id, t_info in tasks_master.items():
        clean_id = t_id
        if clean_id.startswith('M_') or clean_id.startswith('C_'):
            clean_id = clean_id[2:]
        disp_no = clean_id.split('_')[0] if '_' in clean_id else clean_id
        
        d_type = str(t_info.get('DayType', 'All')).strip()
        disp_to_internal[(disp_no.upper(), d_type)] = t_id
        disp_to_internal[(disp_no.upper(), 'All')] = t_id
        internal_to_disp[t_id] = disp_no

    SPECIAL_DUTIES = (
        [f"A{i}" for i in range(1, 8)] +
        [f"J{i}" for i in range(1, 7)] +
        [f"R{i}" for i in range(1, 7)] +
        [f"S{i}" for i in range(1, 4)]
    )

    # 決定変数
    x = {}
    for p in existing_members:
        for d in days:
            for t in all_tasks:
                x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')
                
    # ハード制約1: 1人1日1タスク（絶対強制）
    for p in existing_members:
        for d in days:
            model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

    penalty_terms = []

    # 1. 資格（Role）ミスマッチのペナルティ
    for p in existing_members:
        p_role = member_role.get(p, 'MC')
        for t_id, t_info in tasks_master.items():
            t_role = str(t_info.get('Role', 'All')).strip().upper()
            if (p_role == 'M' and t_role == 'C') or (p_role == 'C' and t_role == 'M'):
                for d in days:
                    penalty_terms.append(x[p, d, t_id] * 10000000)

    # 2. 女性用宿泊設備なしのペナルティ
    for p in existing_members:
        p_gender = member_gender.get(p, 'M')
        if p_gender == 'F':
            for t_id, t_info in tasks_master.items():
                female_ok = str(t_info.get('FemaleAllowed', 'Y')).strip().upper()
                if female_ok == 'N':
                    for d in days:
                        penalty_terms.append(x[p, d, t_id] * 10000000)

    # 3. 日別タスク割り当て数の維持（人数不一致へのゆるいペナルティ）
    for d in days:
        d_type = day_type_map.get(d, 'Weekday')
        day_row = df_initial_shift[df_initial_shift['Date'] == d]
        
        converted_day_tasks = []
        for p in existing_members:
            if not day_row.empty and p in day_row.columns:
                raw_t = str(day_row[p].values[0]).strip().upper()
            else:
                raw_t = 'OFF'
            
            internal_t = disp_to_internal.get((raw_t, d_type), disp_to_internal.get((raw_t, 'All'), raw_t))
            converted_day_tasks.append(internal_t)
            
            if raw_t == 'OFF' or internal_t == 'OFF':
                if 'OFF' in all_tasks:
                    penalty_terms.append((1 - x[p, d, 'OFF']) * 5000000)
            elif raw_t in SPECIAL_DUTIES:
                if internal_t in all_tasks:
                    penalty_terms.append((1 - x[p, d, internal_t]) * 5000000)

        for t in all_tasks:
            target_count = converted_day_tasks.count(t)
            actual_count = sum(x[p, d, t] for p in existing_members)
            diff = model.NewIntVar(-100, 100, f'diff_{d}_{t}')
            model.Add(diff == actual_count - target_count)
            
            diff_abs = model.NewIntVar(0, 100, f'diff_abs_{d}_{t}')
            model.AddAbsEquality(diff_abs, diff)
            penalty_terms.append(diff_abs * 100000)

    # 4. 連続ペアタスク制約
    for d_idx in range(len(days) - 1):
        d_curr = days[d_idx]
        d_next = days[d_idx + 1]
        next_d_type = day_type_map.get(d_next, 'Weekday')
        
        for t_id, t_info in tasks_master.items():
            pair_raw = str(t_info.get('PairTaskID', '')).strip().upper()
            if pair_raw and pair_raw != 'NAN':
                pair_disp = pair_raw.split('_')[0]
                resolved_pair_id = disp_to_internal.get(
                    (pair_disp, next_d_type), 
                    disp_to_internal.get((pair_disp, 'All'), pair_raw)
                )
                
                if resolved_pair_id in tasks_master:
                    for p in existing_members:
                        pair_violated = model.NewBoolVar(f'pv_{p}_{d_curr}_{t_id}')
                        model.Add(x[p, d_curr, t_id] == 1).OnlyEnforceIf(pair_violated.Not())
                        model.Add(x[p, d_next, resolved_pair_id] == 0).OnlyEnforceIf(pair_violated.Not())
                        penalty_terms.append(pair_violated * 1000000)

    # 5. 拠点ミスマッチペナルティ
    for p in existing_members:
        home_st = member_home.get(p, '')
        for d in days:
            for t_id, t_info in tasks_master.items():
                if str(t_info.get('TargetArea', '')).strip().upper() != str(home_st).strip().upper():
                    penalty_terms.append(x[p, d, t_id] * 10000)

    # 6. Late-Early 回避ペナルティ
    for d_idx in range(len(days) - 1):
        d_curr = days[d_idx]
        d_next = days[d_idx + 1]
        for p in existing_members:
            for t1_id, t1_info in tasks_master.items():
                if str(t1_info.get('EndType')).strip() == 'Late':
                    for t2_id, t2_info in tasks_master.items():
                        if str(t2_info.get('StartType')).strip() == 'Early':
                            late_early = model.NewBoolVar(f'le_{p}_{d_curr}_{t1_id}_{t2_id}')
                            model.AddBoolAnd([x[p, d_curr, t1_id], x[p, d_next, t2_id]]).OnlyEnforceIf(late_early)
                            model.AddBoolOr([x[p, d_curr, t1_id].Not(), x[p, d_next, t2_id].Not()]).OnlyEnforceIf(late_early.Not())
                            penalty_terms.append(late_early * 1000)

    # 7. 負荷平準化ペナルティ
    for p in existing_members:
        p_diff = sum(x[p, d, t] * int(tasks_master[t].get('Load', 0)) for d in days for t in all_tasks)
        penalty_terms.append(p_diff)

    model.Minimize(sum(penalty_terms))
    
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60.0
    solver.parameters.num_search_workers = 4
    
    status = solver.Solve(model)
    
    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        result_rows = []
        for d in days:
            row = {'Date': d}
            for p in existing_members:
                p_role = member_role.get(p, 'MC')
                for t in all_tasks:
                    if solver.Value(x[p, d, t]) == 1:
                        disp_no = internal_to_disp.get(t, t)
                        if p_role == 'MC' and (t.startswith('M_') or t.startswith('C_')):
                            suffix = 'M' if t.startswith('M_') else 'C'
                            row[p] = f"{disp_no}{suffix}"
                        else:
                            row[p] = disp_no
                        break
            result_rows.append(row)
        
        df_result_vert = pd.DataFrame(result_rows)
        df_result_horiz = df_result_vert.set_index('Date').T.reset_index()
        df_result_horiz.rename(columns={'index': header_col}, inplace=True)
        
        df_result_horiz.insert(1, 'Name', df_result_horiz[header_col].map(member_names).fillna(''))

        day_type_output_row = {header_col: 'DayType', 'Name': ''}
        for d in days:
            day_type_output_row[d] = day_type_map.get(d, '')
        
        df_dt_row = pd.DataFrame([day_type_output_row])
        df_result_final = pd.concat([df_dt_row, df_result_horiz], ignore_index=True)
        
        return df_result_final, True, "成功"
    else:
        return df_initial_raw, False, "解が見つかりませんでした。"

# アプリメイン画面
if check_password():
    st.title("勤務変更補助システム")
    st.caption("自動シフトトレード・制約最適化ソルバー")

    st.subheader("1. データファイルのアップロード")
    file_members = st.file_uploader("メンバーマスター (Member_Master.csv)", type=["csv"], key="u_members")
    file_tasks = st.file_uploader("仕業マスター (Task_Master.csv)", type=["csv"], key="u_tasks")
    file_initial = st.file_uploader("初期勤務表 (Initial_Schedule.csv)", type=["csv"], key="u_initial")

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行", key="btn_run"):
        if file_members and file_tasks and file_initial:
            progress_msg = st.empty()
            progress_msg.info("⏳ 最適化計算を実行中です...（最大60秒お待ちください）")
            
            try:
                df_m = read_csv_safe(file_members)
                df_t = read_csv_safe(file_tasks)
                df_i = read_csv_safe(file_initial)
                
                result_df, success, msg = run_optimization(df_m, df_t, df_i)
                progress_msg.empty() # メッセージを消去
                
                if success:
                    st.success("🎉 シフトの調整・最適化が正常に完了しました！")
                    csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="📥 調整済みシフト表 (Optimized_Schedule.csv) をダウンロード",
                        data=csv_data,
                        file_name="Optimized_Schedule.csv",
                        mime="text/csv",
                        key="btn_dl"
                    )
                else:
                    st.error(f"最適化失敗: {msg}")
            except Exception as e:
                progress_msg.empty()
                st.error(f"処理中にエラーが発生しました: {str(e)}")
        else:
            st.warning("⚠️ 3つのファイル（メンバーマスター・仕業マスター・初期勤務表）をすべて指定してください。")