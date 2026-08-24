import pandas as pd
from pulp import LpProblem, LpMinimize, LpVariable, lpSum, LpBinary, PULP_CBC_CMD

def run_schedule_optimization():
    # ---------------------------------------------------------
    # 1. データの読み込み
    # ---------------------------------------------------------
    try:
        df_member = pd.read_csv('Member_Master.csv')
        df_initial = pd.read_csv('Initial_Schedule.csv')
        df_task = pd.read_csv('Task_Master.csv')
    except Exception as e:
        print(f"ファイル読み込みエラー: {e}")
        return

    # Member Masterの辞書化
    members_dict = {}
    for _, row in df_member.iterrows():
        m_id = int(row['MemberID'])
        members_dict[m_id] = {
            'Name': str(row['Name']).strip(),
            'BaseArea': str(row['BaseArea']).strip(),
            'Role': str(row['Role']).strip()
        }

    # Initial Schedule の解析
    date_cols = [c for c in df_initial.columns if c not in ['Date', 'Unnamed: 1', '']]
    dates = [str(df_initial[c].iloc[0]).strip() for c in date_cols]
    
    df_schedule_rows = df_initial[df_initial['Date'] != 'DayType'].copy()
    
    member_ids = []
    initial_schedule = {}
    
    for _, row in df_schedule_rows.iterrows():
        try:
            m_id = int(row['Date'])
        except ValueError:
            continue
            
        if m_id in members_dict:
            member_ids.append(m_id)
            for idx, col in enumerate(date_cols):
                d_name = dates[idx]
                val = str(row[col]).strip()
                initial_schedule[(m_id, d_name)] = val

    all_works = list(set(initial_schedule.values()))
    
    # Task Masterから仕業のBaseAreaマッピングを取得
    # TaskID(またはWorkCode) -> TargetArea(またはBaseArea)
    task_area_map = {}
    code_col = 'TaskID' if 'TaskID' in df_task.columns else ('WorkCode' if 'WorkCode' in df_task.columns else None)
    area_col = 'TargetArea' if 'TargetArea' in df_task.columns else ('BaseArea' if 'BaseArea' in df_task.columns else None)
    
    if code_col and area_col:
        for _, row in df_task.iterrows():
            w_code = str(row[code_col]).strip()
            task_area_map[w_code] = str(row[area_col]).strip()

    def get_task_base(w_code):
        if w_code in task_area_map:
            return task_area_map[w_code]
        # 簡易判定（仕業名にヒントがある場合などのバックアップ）
        return 'ANY'

    # ---------------------------------------------------------
    # 2. 最適化モデルの構築
    # ---------------------------------------------------------
    prob = LpProblem("Shift_Optimization", LpMinimize)
    x = LpVariable.dicts("x", (member_ids, dates, all_works), cat=LpBinary)

    # ---------------------------------------------------------
    # 3. 制約条件
    # ---------------------------------------------------------

    # (A) 各メンバー・各日は必ず1仕業
    for m in member_ids:
        for d in dates:
            prob += lpSum([x[m][d][w] for w in all_works]) == 1, f"OneWork_{m}_{d}"

    # (B) 各仕業の必要人数（需要）を維持
    for d in dates:
        for w in all_works:
            initial_count = sum(1 for m in member_ids if initial_schedule[(m, d)] == w)
            prob += lpSum([x[m][d][w] for m in member_ids]) == initial_count, f"Supply_{d}_{w}"

    # (C) OFF（有休等）の完全固定制約
    for m in member_ids:
        for d in dates:
            if initial_schedule[(m, d)] == 'OFF':
                prob += x[m][d]['OFF'] == 1, f"Fix_OFF_{m}_{d}"

    # (D) 役職（Role）適合制約 【絶対条件】
    for m in member_ids:
        m_role = members_dict[m]['Role']
        for d in dates:
            for w in all_works:
                if w in ['OFF', 'A1', 'S2', 'S3', 'J5', 'J6']:
                    continue
                # M専任にC仕業不可 / C専任にM仕業不可
                if w.endswith('M') and m_role == 'C':
                    prob += x[m][d][w] == 0, f"RoleBlock_C_{m}_{d}_{w}"
                elif w.endswith('C') and m_role == 'M':
                    prob += x[m][d][w] == 0, f"RoleBlock_M_{m}_{d}_{w}"

    # (E) 連番ペア制約
    for i in range(len(dates) - 1):
        d_curr = dates[i]
        d_next = dates[i+1]
        
        for m in member_ids:
            for w in all_works:
                prefix = w[:-1]
                suffix = w[-1]
                if prefix.isdigit() and suffix in ['M', 'C']:
                    next_num = int(prefix) + 1
                    next_w = f"{next_num}{suffix}"
                    if next_w in all_works:
                        prob += x[m][d_next][next_w] >= x[m][d_curr][w], f"Pair_{m}_{d_curr}_{w}"

    # ---------------------------------------------------------
    # 4. 目的関数: エリア最適化 ＆ 変更ペナルティのバランス
    # ---------------------------------------------------------
    objective_terms = []

    for m in member_ids:
        m_base = members_dict[m]['BaseArea']
        for d in dates:
            init_w = initial_schedule[(m, d)]
            for w in all_works:
                w_base = get_task_base(w)
                
                # ① エリア不一致に対する強いペナルティ (+1000)
                if w_base != 'ANY' and m_base != 'ANY' and w_base != m_base:
                    objective_terms.append(x[m][d][w] * 1000)
                
                # ② 不要なシフト変更に対する軽微なペナルティ (+1)
                # エリア解消のための変更（コスト-1000の改善）なら＋1のペナルティを余裕で上回る
                if w != init_w:
                    objective_terms.append(x[m][d][w] * 1)

    prob += lpSum(objective_terms)

    # ---------------------------------------------------------
    # 5. 解く
    # ---------------------------------------------------------
    status = prob.solve(PULP_CBC_CMD(msg=False))

    if status != 1:
        print("解が見つからなかったわ。制約条件を再確認してちょうだい。")
        return

    # ---------------------------------------------------------
    # 6. CSV出力
    # ---------------------------------------------------------
    out_rows = []
    header1 = ['Date', ''] + dates
    day_types = [df_initial[c].iloc[0] for c in date_cols]
    header2 = ['DayType', 'Name'] + day_types
    
    out_rows.append(header1)
    out_rows.append(header2)

    for m in member_ids:
        m_name = members_dict[m]['Name']
        row = [m, m_name]
        for d in dates:
            assigned_w = ""
            for w in all_works:
                if x[m][d][w].varValue is not None and x[m][d][w].varValue > 0.5:
                    assigned_w = w
                    break
            row.append(assigned_w)
        out_rows.append(row)

    df_out = pd.DataFrame(out_rows)
    df_out.to_csv('Optimized_Schedule.csv', index=False, header=False, encoding='utf-8-sig')
    print("修正完了よ！『Optimized_Schedule.csv』を出力したわ。")

if __name__ == '__main__':
    run_schedule_optimization()