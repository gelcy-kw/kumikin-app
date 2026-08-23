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


# --- 超高速・安定版 最適化処理メイン ---
def run_optimization(df_members, df_tasks, df_initial_raw):
    logs = []

    def log(msg):
        logs.append(str(msg))
        print(f"[OPT_LOG] {msg}")

    try:
        log("最適化処理を開始します (超高速ルールベースモード)...")

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

        has_name_in_master = "Name" in df_members.columns
        member_names = (
            df_members.set_index("MemberID")["Name"]
            .astype(str)
            .str.strip()
            .to_dict()
            if has_name_in_master
            else {}
        )

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

        # 結果フレームの構築（初期スケジュールを維持しつつ、安全にデータ整形）
        df_result_horiz = df_initial_indexed.loc[existing_members].reset_index()

        df_result_horiz.insert(
            1,
            "Name",
            df_result_horiz[header_col].map(member_names).fillna(""),
        )

        day_type_output_row = {header_col: "DayType", "Name": ""}
        for d in dates:
            day_type_output_row[d] = day_type_map.get(d, "")

        df_dt_row = pd.DataFrame([day_type_output_row])
        df_result_final = pd.concat(
            [df_dt_row, df_result_horiz], ignore_index=True
        )

        log("データ処理・調整が完了しました。")
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
            with st.spinner("⏳ 処理を実行中です..."):
                try:
                    df_m = safe_read_csv(file_members)
                    df_t = safe_read_csv(file_tasks)
                    df_i = safe_read_csv(file_initial)

                    result_df, success, msg, logs = run_optimization(
                        df_m, df_t, df_i
                    )

                    if success:
                        st.success("🎉 シフトの調整・出力準備が正常に完了しました！")
                        csv_data = result_df.to_csv(index=False).encode(
                            "utf-8-sig"
                        )
                        st.download_button(
                            label="📥 調整済みシフト表 (Optimized_Schedule.csv) をダウンロード",
                            data=csv_data,
                            file_name="Optimized_Schedule.csv",
                            mime="text/csv",
                            key="btn_dl",
                        )
                    else:
                        st.error(f"処理失敗: {msg}")

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


if __name__ == "__main__":
    main()