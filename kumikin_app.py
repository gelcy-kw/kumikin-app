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

def normalize_task_id(t_str):
    s = clean_str(t_str).upper()
    if not s or s == 'OFF':
        return None, None, 'OFF'

    if re.match(r'^[AJRS]\d+$', s):
        return None, None, s

    m1 = re.match(r'^(\d+)([MC])$', s)
    if m1:
        num, role = m1.groups()
        return num, role, f"{role}_{num}"

    m2 = re.match(r'^([MC])_(\d+)_?([WH])?$', s)
    if m2:
        role, num, _ = m2.groups()
        return num, role, f"{role}_{num}"

    m3 = re.match(r'^([MC])_(\d+)$', s)
    if m3:
        role, num = m3.groups()
        return num, role, f"{role}_{num}"

    return None, None, s

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
            return df_initial_raw, False, "DayType行なし", [], [], [], []
        
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
            return df_initial_raw, False, "メンバーID不一致", [], [], [], []
            
        df_initial_indexed = df_initial_indexed.loc[existing_members]

        df_initial_shift = df_initial_indexed.T
        df_initial_shift.index = [clean_str(idx) for idx in df_initial_shift.index]
        df_initial_shift = df_initial_shift.reset_index().rename(columns={'index': 'Date'})

        tasks_master = {}
        for _, row in df_tasks.iterrows():
            raw_id = clean_str(row.get('TaskID', ''))
            if not raw_id:
                continue
            info = row.to_dict()
            info['TargetArea'] = clean_str(row.get('TargetArea', ''))
            info['PairTaskID'] = clean_str(row.get('PairTaskID', ''))
            info['DayType'] = clean_str(row.get('DayType', ''))
            info['Role'] = clean_str(row.get('Role', ''))
            info['FemaleAllowed'] = clean_str(row.get('FemaleAllowed', 'Y'))
            tasks_master[raw_id] = info

        def resolve_task_id(raw_str, day_type):
            s = clean_str(raw_str)
            if not s or s == 'OFF':
                return 'OFF'

            if s in tasks_master:
                return s

            num, role, base_key = normalize_task_id(s)
            if base_key == 'OFF' or base_key.startswith(('A', 'J', 'R', 'S')):
                return base_key

            suffix = '_W' if day_type == 'Weekday' else '_H'
            
            if role in ['M', 'C']:
                target_key = f"{role}_{num}{suffix}"
                if target_key in tasks_master:
                    return target_key

            for r in ['M', 'C']:
                target_key = f"{r}_{num}{suffix}"
                if target_key in tasks_master:
                    return target_key

            return s

        def get_disp_name(full_task_id):
            if full_task_id == 'OFF':
                return 'OFF'
            m = re.match(r'^([MC])_(\d+)_([WH])$', full_task_id)
            if m:
                role, num, _ = m.groups()
                return f"{num}{role}"
            return full_task_id

        initial_assignment = {}
        all_tasks_set = set(['OFF'])
        for t_key in tasks_master.keys():
            all_tasks_set.add(t_key)

        unresolved_warnings = []
        for d in dates:
            d_type = day_type_map.get(d, 'Weekday')
            day_row = df_initial_shift[df_initial_shift['Date'] == d]
            
            for p in existing_members:
                if not day_row.empty and p in day_row.columns:
                    raw_t = clean_str(day_row[p].values[0])
                else:
                    raw_t = 'OFF'
                
                resolved_t = resolve_task_id(raw_t, d_type)
                if raw_t != 'OFF' and resolved_t not in tasks_master and not resolved_t.startswith(('A', 'J', 'R', 'S')):
                    unresolved_warnings.append(f"⚠️ {d} の {p} の仕業 '{raw_t}' (解決名: '{resolved_t}') が Task_Master に存在しません！")
                
                initial_assignment[(p, d)] = (raw_t, resolved_t)
                all_tasks_set.add(resolved_t)

        all_tasks = list(all_tasks_set)

        model = cp_model.CpModel()
        x = {}
        for p in existing_members:
            for d in dates:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

        SPECIAL_DUTIES = (
            [f"A{i}" for i in range(1, 8)] +
            [f"J{i}" for i in range(1, 7)] +
            [f"R{i}" for i in range(1, 7)] +
            [f"S{i}" for i in range(1, 4)]
        )

        for d in dates:
            for p in existing_members:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

            for p in existing_members:
                raw_t, resolved_t = initial_assignment[(p, d)]
                if raw_t == 'OFF' or resolved_t == 'OFF':
                    model.Add(x[p, d, 'OFF'] == 1)
                elif raw_t in SPECIAL_DUTIES or resolved_t in SPECIAL_DUTIES:
                    model.Add(x[p, d, resolved_t] == 1)

            tasks_today = [initial_assignment[(p, d)][1] for p in existing_members]
            for t in set(tasks_today):
                count = tasks_today.count(t)
                model.Add(sum(x[p, d, t] for p in existing_members) == count)

        # 属性制約
        for p in existing_members:
            p_gender = clean_str(members_info[p].get('Gender', 'M')).upper()
            p_role = clean_str(members_info[p].get('Role', 'MC')).upper()
            
            for d in dates:
                for t_id in all_tasks:
                    if t_id == 'OFF':
                        continue
                    t_info = tasks_master.get(t_id, {})
                    t_female_allowed = clean_str(t_info.get('FemaleAllowed', 'Y')).upper()
                    t_role = clean_str(t_info.get('Role', 'ALL')).upper()

                    if p_gender == 'F' and t_female_allowed == 'N':
                        model.Add(x[p, d, t_id] == 0)
                        
                    if p_role != 'MC' and t_role not in ['ALL', '']:
                        if p_role == 'M' and t_role == 'C':
                            model.Add(x[p, d, t_id] == 0)
                        elif p_role == 'C' and t_role == 'M':
                            model.Add(x[p, d, t_id] == 0)

        # -------------------------------------------------------------
        # ペア仕業（一泊二日）のハード制約【正方向（当日➔翌日）修正版】
        # -------------------------------------------------------------
        pair_debug_logs = []
        for d_idx in range(len(dates) - 1):
            d_curr = dates[d_idx]
            d_next = dates[d_idx + 1]
            next_d_type = day_type_map.get(d_next, 'Weekday')
            
            # 当日(d_curr)に存在する全仕業について判定
            for t_curr in all_tasks:
                if t_curr == 'OFF':
                    continue
                
                t_info = tasks_master.get(t_curr, {})
                pair_raw = clean_str(t_info.get('PairTaskID', ''))
                
                # PairTaskIDが指定されている場合のみ（例: 39MのPairTaskIDは40）
                if pair_raw and pair_raw not in ['nan', 'None', '']:
                    curr_role = t_info.get('Role', 'M')
                    pair_resolved = resolve_task_id(f"{pair_raw}{curr_role}", next_d_type)
                    
                    if pair_resolved in all_tasks:
                        pair_debug_logs.append(
                            f"【ペア制約適用】{get_disp_name(t_curr)} ({d_curr}) ➔ 翌日必ず {get_disp_name(pair_resolved)} ({d_next})"
                        )
                        # 当日t_currを行うメンバーは、翌日必ずpair_resolvedを行わなければならない
                        for p in existing_members:
                            model.Add(x[p, d_curr, t_curr] == x[p, d_next, pair_resolved])

        # 目的関数（拠点不一致ペナルティ & トレードインセンティブ）
        penalty_terms = []
        score_debug_logs = []

        for p in existing_members:
            home_st = clean_str(members_info[p].get('BaseArea', '')).strip()
            for d in dates:
                raw_t, orig_resolved = initial_assignment[(p, d)]
                if orig_resolved == 'OFF':
                    continue
                
                t_info = tasks_master.get(orig_resolved, {})
                target_area = clean_str(t_info.get('TargetArea', '')).strip()

                for t_id in all_tasks:
                    if t_id == 'OFF':
                        continue
                    t_info_curr = tasks_master.get(t_id, {})
                    t_target = clean_str(t_info_curr.get('TargetArea', '')).strip()
                    
                    if home_st and t_target:
                        if t_target.lower() != home_st.lower():
                            penalty_terms.append(x[p, d, t_id] * 1000)
                        else:
                            penalty_terms.append(x[p, d, t_id] * (-100))

        # トレードを促すためのスコア調整（変更することへの小さなインセンティブ）
        for d in dates:
            for p in existing_members:
                _, orig_resolved = initial_assignment[(p, d)]
                if orig_resolved in all_tasks and orig_resolved != 'OFF':
                    # 元の仕業を維持したら +1 ペナルティ（＝トレードした方がスコアが良くなる）
                    penalty_terms.append(x[p, d, orig_resolved] * 1)

        if penalty_terms:
            model.Minimize(sum(penalty_terms))
        
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
                        if (p, d, t) in x and solver.Value(x[p, d, t]) == 1:
                            assigned_disp = get_disp_name(t)
                            row[p] = assigned_disp
                            
                            orig_raw, orig_resolved = initial_assignment[(p, d)]
                            if t != orig_resolved and orig_resolved != 'OFF':
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
            for d in dates:
                day_type_output_row[d] = day_type_map.get(d, '')
            
            df_dt_row = pd.DataFrame([day_type_output_row])
            df_result_final = pd.concat([df_dt_row, df_result_horiz], ignore_index=True)
            
            return df_result_final, True, "OK", change_logs, pair_debug_logs, score_debug_logs, unresolved_warnings
        else:
            return df_initial_raw, False, f"Solver Status: {solver.StatusName(status)}", [], pair_debug_logs, score_debug_logs, unresolved_warnings

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("トレード判定中..."):
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success, log_msg, change_logs, pair_debug_logs, score_debug_logs, unresolved_warnings = run_optimization(df_m, df_t, df_i)
                
                if unresolved_warnings:
                    st.warning("⚠️ マッチング未解決警告が検知されました:")
                    for w in unresolved_warnings:
                        st.write(w)

                if success:
                    st.success("最適化計算が正常に完了しました！")
                    st.subheader("📋 変更された勤務一覧")
                    if change_logs:
                        for log in change_logs:
                            st.write(log)
                    else:
                        st.info("ℹ️ 条件を満たす効果的なトレードが存在しなかったため、初期シフトを維持しました。")

                    with st.expander("🔍 適用されたペア制約ログ"):
                        for p_log in sorted(list(set(pair_debug_logs))):
                            st.write(p_log)

                    csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="調整済みシフト表(Optimized_Schedule.csv)をダウンロード",
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