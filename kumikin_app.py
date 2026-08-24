import pandas as pd
from datetime import datetime, timedelta
from ortools.sat.python import cp_model

def solve_shift_scheduling():
    # ---------------------------------------------------------
    # 1. データの読み込みと前処理
    # ---------------------------------------------------------
    # ※ファイルパスは環境に合わせて変更してください
    try:
        def load_csv_safely(file_path):
    try:
        return pd.read_csv(file_path, encoding='utf-8')
    except (UnicodeDecodeError, pd.errors.ParserError):
        return pd.read_csv(file_path, encoding='cp932')

df_initial = load_csv_safely('Initial_Schedule.csv')
    except FileNotFoundError:
        print("エラー: Initial_Schedule.csv が見つかりません。")
        return

    # 日付列の型変換（'Date' 列が存在すると仮定）
    df_initial['Date'] = pd.to_datetime(df_initial['Date'])

    # 初期スケジュールの辞書化: {(従業員ID, 日付): 仕業名}
    initial_schedule_dict = {}
    for _, row in df_initial.iterrows():
        emp = row['Employee']
        day = row['Date']
        work = str(row['Work']).strip()
        initial_schedule_dict[(emp, day)] = work

    # ---------------------------------------------------------
    # 2. ペア制約ルールの定義（正しい前後関係）
    #  "前日(1日目)の仕業": "翌日(2日目)に必須となる仕業"
    # ---------------------------------------------------------
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

    # ---------------------------------------------------------
    # 3. Solver & モデルのセットアップ
    # ---------------------------------------------------------
    model = cp_model.CpModel()

    # 全従業員、全日付、全仕業のリストを取得
    employees = sorted(list(set(df_initial['Employee'])))
    target_days = sorted(list(set(df_initial['Date'])))
    
    # 対象となる全仕業（ペアの前後 + 必要に応じて他の仕業を追加）
    all_works = sorted(list(set(list(pair_rules.keys()) + list(pair_rules.values()) + list(df_initial['Work'].astype(str)))))

    # 変数定義: x[emp, day, work] = 1 (割り当てられた場合)
    x = {}
    for emp in employees:
        for day in target_days:
            for work in all_works:
                x[emp, day, work] = model.NewBoolVar(f'x_{emp}_{day.strftime("%Y%m%d")}_{work}')

    # ---------------------------------------------------------
    # 4. 基本制約: 1人1日1仕業
    # ---------------------------------------------------------
    for emp in employees:
        for day in target_days:
            model.AddExactlyOne(x[emp, day, work] for work in all_works)

    # ---------------------------------------------------------
    # 5. 【修正箇所】ペア制約の適用ロジック
    # ---------------------------------------------------------
    print("🔍 デバッグログ（ペア制約の適用状況）")
    pair_constraint_count = 0

    # 全日程を一括処理するのではなく、Initial_Schedule に存在する実データのみを参照
    for (emp, day), prev_work in initial_schedule_dict.items():
        # 実績の仕業がペアの「前半」に合致する場合のみ翌日制約を付与
        if prev_work in pair_rules:
            next_work_required = pair_rules[prev_work]
            next_day = day + timedelta(days=1)

            # 翌日が最適化対象の期間内に含まれているかチェック
            if next_day in target_days:
                # 翌日の仕業を「後半の仕業」に固定
                model.Add(x[emp, next_day, next_work_required] == 1)
                
                print(f"【ペア制約適用】{emp}さん: {prev_work} ({day.strftime('%m月%d日')}) ➔ 翌日必ず {next_work_required} ({next_day.strftime('%m月%d日')})")
                pair_constraint_count += 1

    if pair_constraint_count == 0:
        print("※Initial_Schedule.csv内に該当するペア前半の仕業が見つからなかったため、ペア制約は適用されませんでした。")

    # ---------------------------------------------------------
    # 6. ソルバーの実行
    # ---------------------------------------------------------
    solver = cp_model.CpSolver()
    # タイムアウト設定（必要に応じて調整）
    solver.parameters.max_time_in_seconds = 30.0

    status = solver.Solve(model)

    # ---------------------------------------------------------
    # 7. 結果の出力
    # ---------------------------------------------------------
    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        print("\n✅ 解が見つかりました！")
        results = []
        for emp in employees:
            for day in target_days:
                for work in all_works:
                    if solver.Value(x[emp, day, work]) == 1:
                        results.append({'Employee': emp, 'Date': day.strftime('%Y-%m-%d'), 'Work': work})
        
        df_result = pd.DataFrame(results)
        print(df_result.head(15)) # 最初の15件を表示
        df_result.to_csv('Optimized_Schedule.csv', index=False)
        print("\n'Optimized_Schedule.csv' に出力完了したわ。")
    else:
        print(f"\n❌ 解が見つかりませんでした。（詳細: Solver Status: {solver.StatusName(status)}）")
        print("まだ不可解（INFEASIBLE）が出るなら、ペア制約以外の基本条件（休日日数や連続勤務上限）と競合していないか確認しなさいよね。")

if __name__ == '__main__':
    solve_shift_scheduling()