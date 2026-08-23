import io
import sys
import traceback
import pandas as pd
from ortools.sat.python import cp_model
import streamlit as st

# ページ基本設定
st.set_page_config(page_title="勤務変更補助システム", layout="centered")


# --- CSV安全読み込み関数（パターン2） ---
def safe_read_csv(file):
    """UTF-8 と CP932 (Shift-JIS) の両方に対応して CSV を安全に読み込む"""
    encodings = ["utf-8-sig", "utf-8", "cp932", "shift_jis"]
    for enc in encodings:
        try:
            if hasattr(file, "seek"):
                file.seek(0)
            df = pd.read_csv(file, encoding=enc)
            st.write(f"ℹ️ [LOG] CSVを '{enc}' エンコーディングで読み込みました。")
            return df
        except (UnicodeDecodeError, Exception):
            continue

    # 最終フォールバック
    if hasattr(file, "seek"):
        file.seek(0)
    st.write(
        "⚠️ [LOG] 対応する文字コードで正常にロードできなかったため、cp932 (置換モード) で強制ロードします。"
    )
    return pd.read_csv(file, encoding="cp932", encoding_errors="replace")


# --- 最適化処理メイン ---
def run_optimization(df_members, df_tasks, df_initial_raw):
    logs = []

    def log(msg):
        logs.append(msg)
        print(f"[OPT_LOG] {msg}")

    try:
        log("最適化処理を開始します...")

        # 0. 空列・不要列の除去
        df_initial_raw = df_initial_raw.loc[
            :, ~df_initial_raw.columns.str.contains("^Unnamed")
        ]
        df_initial_raw = df_initial_raw.loc[
            :,
            df_initial_raw.columns.notna() & (df_initial_raw.columns != ""),
        ]

        header_col = df_initial_raw.columns[0]
        log(f"初期スケジュールヘッダー列識別: '{header_col}'")

        # DayType行の取得
        day_types_row = df_initial_raw[
            df_initial_raw[header_col]
            .astype(str)
            .str.strip()
            .str.upper()
            == "DAYTYPE"
        ]
        if day_types_row.empty:
            log("エラー: Initial_Schedule.csv に 'DayType' 行が見つかりません。")
            return (
                df_initial_raw,
                False,
                "Initial_Schedule.csv に 'DayType' 行が見つかりません。",
                logs,
            )

        dates = [str(c).strip() for c in df_initial_raw.columns[1:]]
        log(f"対象日付リスト ({len(dates)}日間): {dates}")

        day_type_map = {}
        for col in df_initial_raw.columns[1:]:
            col_str = str(col).strip()
            day_type_map[col_str] = str(
                day_types_row[col].values[0]
            ).strip()

        # Member_Master 準備
        df_members["MemberID"] = (
            df_members["MemberID"].astype(str).str.strip()
        )
        members = df_members["MemberID"].tolist()
        member_home = (
            df_members.set_index("MemberID")["BaseArea"]
            .astype(str)
            .str.strip()
            .to_dict()
        )

        has_name_in_master = "Name" in df_members.columns
        member_names = (
            df_members.set_index("MemberID")["Name"]
            .astype(str)
            .str.strip()
            .to_dict()
            if has_name_in_master
            else {}
        )

        if "Role" in df_members.columns:
            member_role = (
                df_members.set_index("MemberID")["Role"]
                .astype(str)
                .str.strip()
                .str.upper()
                .to_dict()
            )
        else:
            member_role = {m: "MC" for m in members}

        if "Gender" in df_members.columns:
            member_gender = (
                df_members.set_index("MemberID")["Gender"]
                .astype(str)
                .str.strip()
                .str.upper()
                .to_dict()
            )
        else:
            member_gender = {m: "M" for m in members}

        df_members_sched = df_initial_raw[
            df_initial_raw[header_col]
            .astype(str)
            .str.strip()
            .str.upper()
            != "DAYTYPE"
        ].copy()
        df_members_sched[header_col] = (
            df_members_sched[header_col].astype(str).str.strip()
        )

        df_initial_indexed = df_members_sched.set_index(header_col)
        df_initial_indexed.columns = [
            str(c).strip() for c in df_initial_indexed.columns
        ]

        existing_members = [m for m in members if m in df_initial_indexed.index]
        log(
            f"一致したメンバー数: {len(existing_members)} / 全マスター登録者: {len(members)}"
        )

        if len(existing_members) == 0:
            log(
                "エラー: Initial_Schedule.csv のメンバーIDが Member_Master と一致しません。"
            )
            return (
                df_initial_raw,
                False,
                "Initial_Schedule.csv のメンバーIDが Member_Master と一致しません。",
                logs,
            )

        df_initial_indexed = df_initial_indexed.loc[existing_members]

        df_initial_shift = df_initial_indexed.T
        df_initial_shift.index = [
            str(idx).strip() for idx in df_initial_shift.index
        ]
        df_initial_shift = df_initial_shift.reset_index().rename(
            columns={"index": "Date"}
        )

        model = cp_model.CpModel()
        days = dates

        # Task_Master 準備
        df_tasks["TaskID"] = (
            df_tasks["TaskID"].astype(str).str.strip().str.upper()
        )
        tasks_master = df_tasks.set_index("TaskID").to_dict("index")
        all_tasks = list(tasks_master.keys())
        log(f"タスクマスター登録数: {len(all_tasks)}")

        disp_to_internal = {}
        internal_to_disp = {}
        for t_id, t_info in tasks_master.items():
            clean_id = t_id
            if clean_id.startswith("M_") or clean_id.startswith("C_"):
                clean_id = clean_id[2:]
            disp_no = clean_id.split("_")[0] if "_" in clean_id else clean_id

            d_type = str(t_info.get("DayType", "All")).strip()
            disp_to_internal[(disp_no.upper(), d_type)] = t_id
            disp_to_internal[(disp_no.upper(), "All")] = t_id
            internal_to_disp[t_id] = disp_no

        SPECIAL_DUTIES = (
            [f"A{i}" for i in range(1, 8)]
            + [f"J{i}" for i in range(1, 7)]
            + [f"R{i}" for i in range(1, 7)]
            + [f"S{i}" for i in range(1, 4)]
        )

        # 決定変数定義
        x = {}
        for p in existing_members:
            for d in days:
                for t in all_tasks:
                    x[p, d, t] = model.NewBoolVar(f"x_{p}_{d}_{t}")

        # ハード制約: 1人1日1タスク
        for p in existing_members:
            for d in days:
                model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

        penalty_terms = []

        # 1. 資格ミスマッチペナルティ
        for p in existing_members:
            p_role = member_role.get(p, "MC")
            for t_id, t_info in tasks_master.items():
                t_role = str(t_info.get("Role", "All")).strip().upper()
                if (p_role == "M" and t_role == "C") or (
                    p_role == "C" and t_role == "M"
                ):
                    for d in days:
                        penalty_terms.append(x[p, d, t_id] * 10000000)

        # 2. 女性専用設備ペナルティ
        for p in existing_members:
            p_gender = member_gender.get(p, "M")
            if p_gender == "F":
                for t_id, t_info in tasks_master.items():
                    female_ok = (
                        str(t_info.get("FemaleAllowed", "Y")).strip().upper()
                    )
                    if female_ok == "N":
                        for d in days:
                            penalty_terms.append(x[p, d, t_id] * 10000000)

        # 3. タスク人数の維持（ペナルティ）
        for d in days:
            d_type = day_type_map.get(d, "Weekday")
            day_row = df_initial_shift[df_initial_shift["Date"] == d]

            converted_day_tasks = []
            for p in existing_members:
                if not day_row.empty and p in day_row.columns:
                    raw_t = str(day_row[p].values[0]).strip().upper()
                else:
                    raw_t = "OFF"

                internal_t = disp_to_internal.get(
                    (raw_t, d_type),
                    disp_to_internal.get((raw_t, "All"), raw_t),
                )
                converted_day_tasks.append(internal_t)

                if raw_t == "OFF" or internal_t == "OFF":
                    if "OFF" in all_tasks:
                        penalty_terms.append((1 - x[p, d, "OFF"]) * 5000000)
                elif raw_t in SPECIAL_DUTIES:
                    if internal_t in all_tasks:
                        penalty_terms.append(
                            (1 - x[p, d, internal_t]) * 5000000
                        )

            for t in all_tasks:
                target_count = converted_day_tasks.count(t)
                actual_count = sum(x[p, d, t] for p in existing_members)
                diff = model.NewIntVar(-100, 100, f"diff_{d}_{t}")
                model.Add(diff == actual_count - target_count)

                diff_abs = model.NewIntVar(0, 100, f"diff_abs_{d}_{t}")
                model.AddAbsEquality(diff_abs, diff)
                penalty_terms.append(diff_abs * 100000)

        # 4. 連続ペアタスク制約
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            next_d_type = day_type_map.get(d_next, "Weekday")

            for t_id, t_info in tasks_master.items():
                pair_raw = str(t_info.get("PairTaskID", "")).strip().upper()
                if pair_raw and pair_raw != "NAN":
                    pair_disp = pair_raw.split("_")[0]
                    resolved_pair_id = disp_to_internal.get(
                        (pair_disp, next_d_type),
                        disp_to_internal.get((pair_disp, "All"), pair_raw),
                    )

                    if resolved_pair_id in tasks_master:
                        for p in existing_members:
                            pair_violated = model.NewBoolVar(
                                f"pv_{p}_{d_curr}_{t_id}"
                            )
                            model.Add(x[p, d_curr, t_id] == 1).OnlyEnforceIf(
                                pair_violated.Not()
                            )
                            model.Add(
                                x[p, d_next, resolved_pair_id] == 0
                            ).OnlyEnforceIf(pair_violated.Not())
                            penalty_terms.append(pair_violated * 1000000)

        # 5. 拠点ペナルティ
        for p in existing_members:
            home_st = member_home.get(p, "")
            for d in days:
                for t_id, t_info in tasks_master.items():
                    if (
                        str(t_info.get("TargetArea", "")).strip().upper()
                        != str(home_st).strip().upper()
                    ):
                        penalty_terms.append(x[p, d, t_id] * 10000)

        # 6. Late-Early 回避ペナルティ
        for d_idx in range(len(days) - 1):
            d_curr = days[d_idx]
            d_next = days[d_idx + 1]
            for p in existing_members:
                for t1_id, t1_info in tasks_master.items():
                    if str(t1_info.get("EndType")).strip() == "Late":
                        for t2_id, t2_info in tasks_master.items():
                            if str(t2_info.get("StartType")).strip() == "Early":
                                late_early = model.NewBoolVar(
                                    f"le_{p}_{d_curr}_{t1_id}_{t2_id}"
                                )
                                model.AddBoolAnd(
                                    [x[p, d_curr, t1_id], x[p, d_next, t2_id]]
                                ).OnlyEnforceIf(late_early)
                                model.AddBoolOr(
                                    [
                                        x[p, d_curr, t1_id].Not(),
                                        x[p, d_next, t2_id].Not(),
                                    ]
                                ).OnlyEnforceIf(late_early.Not())
                                penalty_terms.append(late_early * 1000)

        # 7. 負荷平準化
        for p in existing_members:
            p_diff = sum(
                x[p, d, t] * int(tasks_master[t].get("Load", 0))
                for d in days
                for t in all_tasks
            )
            penalty_terms.append(p_diff)

        model.Minimize(sum(penalty_terms))

        log("ソルバーの実行を開始します...")
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60.0
        solver.parameters.num_search_workers = 4

        status = solver.Solve(model)
        log(f"ソルバー実行ステータス: {solver.StatusName(status)}")

        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            result_rows = []
            for d in days:
                row = {"Date": d}
                for p in existing_members:
                    p_role = member_role.get(p, "MC")
                    for t in all_tasks:
                        if solver.Value(x[p, d, t]) == 1:
                            disp_no = internal_to_disp.get(t, t)
                            if p_role == "MC" and (
                                t.startswith("M_") or t.startswith("C_")
                            ):
                                suffix = "M" if t.startswith("M_") else "C"
                                row[p] = f"{disp_no}{suffix}"
                            else:
                                row[p] = disp_no
                            break
                result_rows.append(row)

            df_result_vert = pd.DataFrame(result_rows)
            df_result_horiz = df_result_vert.set_index("Date").T.reset_index()
            df_result_horiz.rename(columns={"index": header_col}, inplace=True)

            df_result_horiz.insert(
                1,
                "Name",
                df_result_horiz[header_col].map(member_names).fillna(""),
            )

            day_type_output_row = {header_col: "DayType", "Name": ""}
            for d in days:
                day_type_output_row[d] = day_type_map.get(d, "")

            df_dt_row = pd.DataFrame([day_type_output_row])
            df_result_final = pd.concat(
                [df_dt_row, df_result_horiz], ignore_index=True
            )

            log("最適化計算に成功しました。")
            return df_result_final, True, "成功", logs
        else:
            log("最適解/実行可能解が見つかりませんでした。")
            return (
                df_initial_raw,
                False,
                "解が見つかりませんでした。",
                logs,
            )

    except Exception as e:
        err_msg = traceback.format_exc()
        log(f"例外エラーが発生しました:\n{err_msg}")
        return df_initial_raw, False, str(e), logs


# --- Streamlit メインUI ---
st.title("勤務変更補助システム")
st.caption("自動シフトトレード・制約最適化ソルバー")

st.subheader("1. データファイルのアップロード")
file_members = st.file_uploader(
    "メンバーマスター (Member_Master.csv)", type=["csv"], key="u_members"
)
file_tasks = st.file_uploader(
    "仕業マスター (Task_Master.csv)", type=["csv"], key="u_tasks"
)
file_initial = st.file_uploader(
    "初期勤務表 (Initial_Schedule.csv)", type=["csv"], key="u_initial"
)

st.subheader("2. 最適化計算の実行")
if st.button("シフト最適化の実行", key="btn_run"):
    if file_members and file_tasks and file_initial:
        with st.spinner("⏳ 最適化計算を実行中です..."):
            try:
                st.info("📂 CSVファイルの読み込みを開始します...")
                df_m = safe_read_csv(file_members)
                df_t = safe_read_csv(file_tasks)
                df_i = safe_read_csv(file_initial)

                st.info("🧠 最適化ソルバーを呼び出しています...")
                result_df, success, msg, logs = run_optimization(
                    df_m, df_t, df_i
                )

                if success:
                    st.success("🎉 シフトの調整・最適化が正常に完了しました！")
                    csv_data = result_df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        label="📥 調整済みシフト表 (Optimized_Schedule.csv) をダウンロード",
                        data=csv_data,
                        file_name="Optimized_Schedule.csv",
                        mime="text/csv",
                        key="btn_dl",
                    )
                else:
                    st.error(f"最適化失敗: {msg}")

                # 実行ログの表示領域
                with st.expander("🔍 詳細ログを表示"):
                    st.text("\n".join(logs))

            except Exception as e:
                st.error(f"処理中に予期せぬエラーが発生しました: {str(e)}")
                with st.expander("🚨 エラートレースバック"):
                    st.code(traceback.format_exc())
    else:
        st.warning(
            "⚠️ 3つのファイル（メンバーマスター・仕業マスター・初期勤務表）をすべて指定してください。"
        )