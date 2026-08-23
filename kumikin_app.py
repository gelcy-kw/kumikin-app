import io
import sys
import traceback
import pandas as pd
import streamlit as st

# ページ基本設定
st.set_page_config(page_title="勤務変更補助システム", layout="centered")


# --- CSV安全読み込み関数 ---
def safe_read_csv(file):
    """UTF-8 と CP932 (Shift-JIS) の両方に対応して CSV を安全に読み込む"""
    encodings = ["utf-8-sig", "utf-8", "cp932", "shift_jis"]
    for enc in encodings:
        try:
            if hasattr(file, "seek"):
                file.seek(0)
            df = pd.read_csv(file, encoding=enc)
            return df
        except Exception:
            continue

    if hasattr(file, "seek"):
        file.seek(0)
    return pd.read_csv(file, encoding="cp932", encoding_errors="replace")


# --- シフトトレード＆最適化処理 ---
def run_optimization(df_members, df_tasks, df_initial_raw):
    logs = []

    def log(msg):
        logs.append(str(msg))
        print(f"[OPT_LOG] {msg}")

    try:
        log("最適化処理（トレード実行モード）を開始します...")

        # 1. ヘッダー・不要列の整理
        df_initial_raw = df_initial_raw.loc[
            :, ~df_initial_raw.columns.str.contains("^Unnamed")
        ]
        df_initial_raw = df_initial_raw.loc[
            :, df_initial_raw.columns.notna() & (df_initial_raw.columns != "")
        ]

        header_col = df_initial_raw.columns[0]
        
        # DayType行の特定
        day_types_row = df_initial_raw[
            df_initial_raw[header_col].astype(str).str.strip().str.upper() == "DAYTYPE"
        ]
        
        dates = [str(c).strip() for c in df_initial_raw.columns[1:]]

        day_type_map = {}
        if not day_types_row.empty:
            for col in df_initial_raw.columns[1:]:
                col_str = str(col).strip()
                day_type_map[col_str] = str(day_types_row[col].values[0]).strip()

        # 2. メンバー情報の整理
        df_members["MemberID"] = df_members["MemberID"].astype(str).str.strip()
        members = df_members["MemberID"].tolist()

        member_names = (
            df_members.set_index("MemberID")["Name"].astype(str).str.strip().to_dict()
            if "Name" in df_members.columns
            else {}
        )

        # 初期スケジュールをメンバーごとに抽出
        df_members_sched = df_initial_raw[
            df_initial_raw[header_col].astype(str).str.strip().str.upper() != "DAYTYPE"
        ].copy()
        df_members_sched[header_col] = df_members_sched[header_col].astype(str).str.strip()

        df_sched = df_members_sched.set_index(header_col)
        df_sched.columns = [str(c).strip() for c in df_sched.columns]

        existing_members = [m for m in members if m in df_sched.index]
        log(f"対象メンバー数: {len(existing_members)} 名 / 対象日数: {len(dates)} 日")

        # 3. トレード（シフト入れ替え）アルゴリズムの実行
        # 各日付ごとにトレード調整を実施
        trade_count = 0
        
        for d in dates:
            # 該当日の各メンバーのシフトを取得
            day_shifts = df_sched[d].to_dict()
            
            # 調整が必要なタスク（「希望休」「要交代」「休」等の特定のフラグ、または特定の未割り当て）
            # ここでは交換可能な2名のシフトを安全にトレードするルールを適用
            unassigned_or_request = [
                m for m, task in day_shifts.items() 
                if str(task).strip() in ["希望休", "要交代", "NG", "休", "OFF", "-"]
            ]
            
            candidates = [
                m for m, task in day_shifts.items() 
                if str(task).strip() not in ["希望休", "要交代", "NG", "休", "OFF", "-", "不可"]
            ]
            
            # トレードペアの検索と交換実行
            for m_req in unassigned_or_request:
                if not candidates:
                    break
                # 代替可能な候補者をピックアップしてシフトをトレード
                m_cand = candidates.pop(0)
                
                # シフトの交換
                task_req = df_sched.at[m_req, d]
                task_cand = df_sched.at[m_cand, d]
                
                df_sched.at[m_req, d] = task_cand
                df_sched.at[m_cand, d] = task_req
                
                trade_count += 1
                log(f"【トレード発生】日付: {d} | メンバー {m_req} ({task_req}) ↔ メンバー {m_cand} ({task_cand})")

        log(f"合計 {trade_count} 件のトレード処理が完了しました。")

        # 4. 結果データフレームの構築
        df_result_horiz = df_sched.loc[existing_members].reset_index()
        df_result_horiz.insert(
            1, "Name", df_result_horiz[header_col].map(member_names).fillna("")
        )

        day_type_output_row = {header_col: "DayType", "Name": ""}
        for d in dates:
            day_type_output_row[d] = day_type_map.get(d, "")

        df_dt_row = pd.DataFrame([day_type_output_row])
        df_result_final = pd.concat([df_dt_row, df_result_horiz], ignore_index=True)

        return df_result_final, True, "成功", logs

    except Exception as e:
        err_msg = traceback.format_exc()
        log(f"例外エラーが発生しました:\n{err_msg}")
        return df_initial_raw, False, str(e), logs


# --- Streamlit メインUI ---
def main():
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
            with st.spinner("⏳ トレード最適化を実行中です..."):
                try:
                    df_m = safe_read_csv(file_members)
                    df_t = safe_read_csv(file_tasks)
                    df_i = safe_read_csv(file_initial)

                    result_df, success, msg, logs = run_optimization(
                        df_m, df_t, df_i
                    )

                    if success:
                        st.success("🎉 シフトのトレード調整・出力が完了しました！")
                        csv_data = result_df.to_csv(index=False).encode("utf-8-sig")
                        st.download_button(
                            label="📥 調整済みシフト表 (Optimized_Schedule.csv) をダウンロード",
                            data=csv_data,
                            file_name="Optimized_Schedule.csv",
                            mime="text/csv",
                            key="btn_dl",
                        )
                    else:
                        st.error(f"処理失敗: {msg}")

                    with st.expander("🔍 詳細ログ・トレード履歴を表示"):
                        st.text("\n".join(logs))

                except Exception as e:
                    st.error(f"処理中に予期せぬエラーが発生しました: {str(e)}")
                    with st.expander("🚨 エラートレースバック"):
                        st.code(traceback.format_exc())
        else:
            st.warning(
                "⚠️ 3つのファイル（メンバーマスター・仕業マスター・初期勤務表）をすべて指定してください。"
            )


if __name__ == "__main__":
    main()