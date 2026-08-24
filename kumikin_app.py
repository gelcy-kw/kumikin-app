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

if check_password():
    # --- メイン画面UI ---
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
            st.error("Initial_Schedule.csv に 'DayType' 行が見つかりません。")
            return df_initial_raw, False
        
        # 日付ごとの DayType マッピング
        dates = [str(c).strip() for c in df_initial_raw.columns[1:]]
        day_type_map = {}
        for col in df_initial_raw.columns[1:]:
            col_str = str(col).strip()
            day_type_map[col_str] = str(day_types_row[col].values[0]).strip()

        # Member_Master のメンバー情報取得
        df_members['MemberID'] = df_members['MemberID'].astype(str).str.strip()
        members_info = df_members.set_index('MemberID').to_dict('index')
        members = list(members_info.keys())

        # メンバーの行のみを安全に抽出（DayType行を除外）
        df_members_sched = df_initial_raw[df_initial_raw[header_col].astype(str).str.strip() != 'DayType'].copy()
        df_members_sched[header_col] = df_members_sched[header_col].astype(str).str.strip()

        # 縦書き(行:日付, 列:人員)構造を作成
        df_initial_indexed = df_members_sched.set_index(header_col)
        df_initial_indexed.columns = [str(c).strip() for c in df_initial_indexed.columns]
        
        existing_members = [m for m in members if m in df_initial_indexed.index]
        if len(existing_members) == 0:
            st.error("Initial_Schedule.csv のメンバーIDが Member_Master と一致しません。")
            return df_initial_raw, False
            
        df_initial_indexed = df_initial_indexed.loc[existing_members]

        # 転置して (行: Date, 列: MemberID...) にする
        df_initial_shift = df_initial_indexed.T
        df_initial_shift.index = [str(idx).strip() for idx in df_initial_shift.index]
        df_initial_shift = df_initial_shift.reset_index().rename(columns={'index': 'Date'})

        model = cp_model.CpModel()
        days = dates
        
        # タスクマスターの準備
        df_tasks['TaskID'] = df_tasks['TaskID'].astype(str).str.strip()
        tasks_master = df_tasks.set_index('TaskID').to_dict('index')
        all_tasks = list(tasks_master.keys())

        # 表示用ID(101_W / M_1_W -> M_1 など)から内部ID(M_1_W)へのマッピング
        disp_to_internal = {}
        internal_to_disp = {}
        for t_id, t_info in tasks_master.items():
            parts = t_id.split('_')
            disp_no = f"{parts[0]}_{parts[1]}" if len(parts) >= 3 else parts[0]
            d_type = str(t_info.get('DayType', 'All')).strip()
            
            disp_to_internal[(disp_no, d_type)] = t_id
            disp_to_internal[(disp_no, 'All')] = t_id
            disp_to_internal[(t_id, d_type)] = t_id
            disp_to_internal[(t_id, 'All')] = t_id
            
            internal_to_disp[t_id] = disp_no

        # 特殊仕業（固定対象）のリスト定義
        SPECIAL_DUTIES = (
            [f"A{i}" for i in range(1, 8)] +
            [f"J{i}" for i in range(1, 7)] +
            [f"R{i}" for i in range(1, 7)] +
            [f"S{i}" for i in range(1, 4)]
        )

        # 決定変数: x[p, d, t]
        x = {}
        for p in existing_members:
            for d in days:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')
                    
        # ハード制約0: 属性・資格による不適合タスクの割り当て事前ブロック
        for p in existing_members:
            p_gender = str(members_info[p].get('Gender', 'M')).strip()
            p_role = str(members_info[p].get('Role', 'MC')).strip()
            
            for t_id, t_info in tasks_master.items():
                if t_id == 'OFF':
                    continue
                    
                t_female_allowed = str(t_info.get('FemaleAllowed', 'Y')).strip()
                t_role = str(t_info.get('Role', 'All')).strip()
                
                # 1. 女性制限ブロック（女性かつFemaleAllowed == 'N'）
                if p_gender == 'F' and t_female_allowed == 'N':
                    for d in days:
                        model.Add(x[p, d, t_id] == 0)
                        
                # 2. 資格制限ブロック
                # 運転士(M)に車掌(C)仕業は不可
                if p_role == 'M' and t_role == 'C':
                    for d in days:
                        model.Add(x[p, d, t_id] == 0)
                # 車掌(C)に運転士(M)仕業は不可
                elif p_role == 'C' and t_role == 'M':
                    for d in days:
                        model.Add(x[p, d, t_id] == 0)

        # ハード制約1: 1人1日1タスク
        for p in existing_members:
            for d in days:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)
                
        # ハード制約2: 日別タスク割り当て数の維持 ＆ 特殊仕業・OFF・Fixed(固定日)の絶対ロック
        for d in days:
            d_type = day_type_map.get(d, 'Weekday')
            day_row = df_initial_shift[df_initial_shift['Date'] == d]
            
            converted_day_tasks = []
            for p in existing_members:
                if not day_row.empty and p in day_row.columns:
                    raw_t = str(day_row[p].values[0]).strip()
                else:
                    raw_t = 'OFF'
                
                # 内部IDを取得
                internal_t = disp_to_internal.get((raw_t, d_type), disp_to_internal.get((raw_t, 'All'), raw_t))
                converted_day_tasks.append(internal_t)
                
                # 1. OFF セルの絶対固定（移動不可）
                if raw_t == 'OFF' or internal_t == 'OFF':
                    if 'OFF' in all_tasks:
                        model.Add(x[p, d, 'OFF'] == 1)

                # 2. 特殊仕業（A1~A7, J1~J6, R1~R6, S1~S3）の絶対固定
                elif raw_t in SPECIAL_DUTIES or internal_t in SPECIAL_DUTIES:
                    if internal_t in all_tasks:
                        model.Add(x[p, d, internal_t] == 1)

                # 3. Fixed (トレード対象外の日) の初期配置固定
                elif d_type == 'Fixed':
                    if internal_t in all_tasks:
                        model.Add(x[p, d, internal_t] == 1)

            # タスク数の維持
            for t in all_tasks:
                count = converted_day_tasks.count(t)
                model.Add(sum(x[p, d, t] for p in existing_members) == count)

        # ハード制約3: 連続ペアタスク制約（翌日の DayType に合わせて自動動的変換）
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            next_d_type = day_type_map.get(d_next, 'Weekday')
            
            for t_id, t_info in tasks_master.items():
                pair_raw = str(t_info.get('PairTaskID', '')).strip()
                if pair_raw and pair_raw != 'nan':
                    t_role_prefix = "M_" if t_id.startswith("M_") else ("C_" if t_id.startswith("C_") else "")
                    pair_disp = f"{t_role_prefix}{pair_raw}" if t_role_prefix and not pair_raw.startswith(t_role_prefix) else pair_raw
                    
                    resolved_pair_id = disp_to_internal.get(
                        (pair_disp, next_d_type), 
                        disp_to_internal.get((pair_disp, 'All'), pair_raw)
                    )
                    
                    if resolved_pair_id in tasks_master:
                        for p in existing_members:
                            model.Add(x[p, d_curr, t_id] == x[p, d_next, resolved_pair_id])

        # 目的関数（ペナルティ項の最小化）
        penalty_terms = []
        
        # 優先順位 1: 拠点ミスマッチペナルティ [重み: 1,000,000]
        for p in existing_members:
            home_st = str(members_info[p].get('BaseArea', '')).strip()
            for d in days:
                for t_id, t_info in tasks_master.items():
                    if t_id == 'OFF':
                        continue
                    if str(t_info['TargetArea']).strip() != home_st:
                        penalty_terms.append(x[p, d, t_id] * 1000000)

        # 優先順位 2: Late-Early（おそはや）回避ペナルティ [重み: 1,000]
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

        # 優先順位 3: 負荷平準化ペナルティ [重み: 1]
        for p in existing_members:
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
                for p in existing_members:
                    for t in all_tasks:
                        if solver.Value(x[p, d, t]) == 1:
                            row[p] = internal_to_disp.get(t, t)
                            break
                result_rows.append(row)
            
            # 横書きフォーマットに再転置して出力
            df_result_vert = pd.DataFrame(result_rows)
            df_result_horiz = df_result_vert.set_index('Date').T.reset_index()
            df_result_horiz.rename(columns={'index': header_col}, inplace=True)
            
            # 先頭に DayType 行を復元
            day_type_output_row = {header_col: 'DayType'}
            for d in days:
                day_type_output_row[d] = day_type_map.get(d, '')
            
            df_dt_row = pd.DataFrame([day_type_output_row])
            df_result_final = pd.concat([df_dt_row, df_result_horiz], ignore_index=True)
            
            return df_result_final, True
        else:
            return df_initial_raw, False

    st.subheader("2. 最適化計算の実行")
    if st.button("シフト最適化の実行"):
        if file_members and file_tasks and file_initial:
            with st.spinner("制約条件を計算中..."):
                # 安全な読み込み関数を使用
                df_m = load_csv_safely(file_members)
                df_t = load_csv_safely(file_tasks)
                df_i = load_csv_safely(file_initial)
                
                result_df, success = run_optimization(df_m, df_t, df_i)
                
                if success:
                    st.success("最適化計算が正常に完了しました。")
                else:
                    st.warning("最適な解が見つかりませんでした。初期シフトを維持します。")
                
                csv_data = result_df.to_csv(index=False).encode('utf-8-sig')
                st.download_button(
                    label="調整済みシフト表(Optimized_Schedule.csv)をダウンロード",
                    data=csv_data,
                    file_name="Optimized_Schedule.csv",
                    mime="text/csv"
                )
        else:
            st.error("エラー: 3つのファイル（メンバーマスター・仕業マスター・初期勤務表）をすべて指定してください。")