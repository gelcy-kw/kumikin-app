def run_optimization(df_members, df_tasks, df_initial_raw):
        debug_logs = []
        def log(msg):
            debug_logs.append(msg)

        try:
            log("--- 最適化処理を開始します ---")
            id_col_name = df_initial_raw.columns[0]
            name_col_name = df_initial_raw.columns[1]
            log(f"ID列名: '{id_col_name}', 名前列名: '{name_col_name}'")

            all_cols = list(df_initial_raw.columns)
            meta_cols = [id_col_name, name_col_name]
            
            for col in all_cols:
                c_upper = col.upper().strip()
                if c_upper in ['OF_M1', 'OF_M2']:
                    meta_cols.append(col)

            # 🔥 修正: UNNAMED列（余計な空列）や空文字を除外して正しい日付だけを抽出
            dates = []
            for c in all_cols:
                c_clean = clean_str(c)
                if c not in meta_cols and not c_clean.startswith('UNNAMED') and c_clean != '':
                    dates.append(c_clean)

            log(f"検出された対象日付（{len(dates)}件）: {dates}")

            lock_row = df_initial_raw[df_initial_raw[id_col_name].apply(clean_str) == 'LOCK']
            day_lock_flags = {}
            for d in dates:
                if not lock_row.empty and d in lock_row.columns:
                    val = clean_str(lock_row.iloc[0][d])
                    day_lock_flags[d] = val in ['LOCK', '1', 'YES', 'TRUE', '固定']
                else:
                    day_lock_flags[d] = False

            daytype_row = df_initial_raw[df_initial_raw[id_col_name].apply(clean_str) == 'DAYTYPE']

            df_members['MemberID'] = df_members['MemberID'].apply(clean_str)
            member_base_area = {}
            member_role = {}
            member_gender = {}
            
            for _, row in df_members.iterrows():
                m_id = clean_str(row['MemberID'])
                area = normalize_area_dynamic(row.get('BaseArea', ''))
                role = clean_str(row.get('Role', ''))
                gender = clean_str(row.get('Gender', ''))
                
                member_base_area[m_id] = area
                member_role[m_id] = role
                member_gender[m_id] = gender

            members = list(member_base_area.keys())
            log(f"メンバーマスター件数: {len(members)}名")

            task_area_map = {}
            task_female_allowed_map = {}
            task_trade_allowed_map = {}
            task_start_type = {}
            task_end_type = {}
            pair_rules = {}

            if 'TaskID' in df_tasks.columns:
                for _, row in df_tasks.iterrows():
                    t_id = clean_str(row['TaskID'])
                    t_area = normalize_area_dynamic(row.get('TargetArea', ''))
                    f_allowed = clean_str(row.get('FemaleAllowed', 'Y'))
                    
                    if 'TradeAllowed' in df_tasks.columns:
                        trade_allowed = parse_trade_allowed(row.get('TradeAllowed'))
                    else:
                        trade_allowed = 'N' if (t_id and not t_id[0].isdigit()) else 'Y'

                    pair_id = clean_str(row.get('PairTaskID', ''))

                    s_type = clean_str(row.get('StartType', ''))
                    e_type = clean_str(row.get('EndType', ''))

                    task_area_map[t_id] = t_area
                    task_female_allowed_map[t_id] = f_allowed
                    task_trade_allowed_map[t_id] = trade_allowed
                    task_start_type[t_id] = s_type
                    task_end_type[t_id] = e_type

                    m_match = re.match(r'^(\d+)([MC])$', t_id)
                    if m_match:
                        role_char = m_match.group(2)
                        if pair_id and pair_id.isdigit():
                            prev_plain_id = f"{pair_id}{role_char}"
                            pair_rules[prev_plain_id] = t_id

                    m_match_long = re.match(r'([MC])_(\d+)_[WH]', t_id)
                    if not m_match_long:
                        m_match_long = re.match(r'([MC])_(\d+)', t_id)
                    if m_match_long:
                        role_char = m_match_long.group(1)
                        num_str = m_match_long.group(2)
                        plain_id = f"{num_str}{role_char}"
                        
                        task_area_map[plain_id] = t_area
                        task_female_allowed_map[plain_id] = f_allowed
                        task_trade_allowed_map[plain_id] = trade_allowed
                        task_start_type[plain_id] = s_type
                        task_end_type[plain_id] = e_type

                        if pair_id and pair_id.isdigit():
                            prev_plain_id = f"{pair_id}{role_char}"
                            pair_rules[prev_plain_id] = plain_id

            for _, row in df_tasks.iterrows():
                t_id = clean_str(row['TaskID'])
                pair_id = clean_str(row.get('PairTaskID', ''))
                if pair_id and not pair_id.isdigit():
                    pair_rules[pair_id] = t_id

            log(f"構築されたペア制約数: {len(pair_rules)}件")

            first_day_pair_tasks = set(pair_rules.keys())
            second_day_pair_tasks = set(pair_rules.values())

            def get_task_area(task_code):
                if is_off_or_vacation(task_code):
                    return 'ANY'
                return task_area_map.get(task_code, 'ANY')

            def is_female_allowed(task_code):
                return task_female_allowed_map.get(task_code, 'Y') != 'N'

            def is_trade_allowed(task_code):
                if is_off_or_vacation(task_code):
                    return False
                if task_code not in task_trade_allowed_map:
                    if task_code and not task_code[0].isdigit():
                        return False
                    return True
                return task_trade_allowed_map.get(task_code, 'Y') == 'Y'

            ignored_rows = ['DAYTYPE', 'LOCK']
            df_sched = df_initial_raw[~df_initial_raw[id_col_name].apply(clean_str).isin(ignored_rows)].copy()
            df_sched[id_col_name] = df_sched[id_col_name].apply(clean_str)

            # 🔥 修正: 列名もキレイにしてからインデックス化する
            df_sched.columns = [clean_str(c) for c in df_sched.columns]
            clean_id_col = clean_str(id_col_name)

            member_names = {}
            member_past_overflow = {}

            col_m1 = next((c for c in df_sched.columns if c == 'OF_M1'), None)
            col_m2 = next((c for c in df_sched.columns if c == 'OF_M2'), None)

            for _, row in df_sched.iterrows():
                m_id = clean_str(row[clean_id_col])
                m_name = str(row[clean_str(name_col_name)]).strip() if pd.notna(row[clean_str(name_col_name)]) else m_id
                member_names[m_id] = m_name

                of1 = parse_int_safely(row[col_m1]) if col_m1 else 0
                of2 = parse_int_safely(row[col_m2]) if col_m2 else 0
                member_past_overflow[m_id] = of1 + of2

            df_initial_indexed = df_sched.set_index(clean_id_col)
            
            existing_members = [m for m in members if m in df_initial_indexed.index]
            log(f"初期勤務表に存在する有効メンバー数: {len(existing_members)}名")

            if len(existing_members) == 0:
                log("エラー: 初期勤務表のIDとメンバーマスターのIDが一致しません。")
                return df_initial_raw, False, "メンバーIDが一致しませんでした", [], [], [], set(), set(), id_col_name, {}, debug_logs

            initial_assignment = {}
            all_tasks_set = set()
            tasks_by_day = {}

            for d in dates:
                tasks_by_day[d] = set()
                for p in existing_members:
                    val = str(df_initial_indexed.loc[p, d]).strip() if pd.notna(df_initial_indexed.loc[p, d]) else '公休'
                    if not val:
                        val = '公休'
                    val = clean_str(val)
                    initial_assignment[(p, d)] = val
                    all_tasks_set.add(val)
                    tasks_by_day[d].add(val)

            all_tasks = list(all_tasks_set)
            log(f"検出されたユニーク仕業数: {len(all_tasks)}件")

            model = cp_model.CpModel()
            x = {}
            for p in existing_members:
                for d in dates:
                    for t in all_tasks:
                        x[p, d, t] = model.NewBoolVar(f'x_{p}_{d}_{t}')

            for d in dates:
                for p in existing_members:
                    model.Add(sum(x[p, d, t] for t in all_tasks) == 1)

            first_date = dates[0] if dates else None
            last_date = dates[-1] if dates else None

            for p in existing_members:
                for d in dates:
                    orig_t = initial_assignment.get((p, d), '公休')
                    
                    is_last_day_pair_first = (d == last_date and orig_t in first_day_pair_tasks)
                    is_first_day_pair_second = (d == first_date and orig_t in second_day_pair_tasks)

                    if not is_trade_allowed(orig_t) or day_lock_flags.get(d, False) or is_last_day_pair_first or is_first_day_pair_second:
                        if is_last_day_pair_first:
                            log(f"🔒【月末保留保護】{d} の {p}さん({member_names.get(p, p)}) の『{orig_t}』は翌月連動なしのため固定化されました。")
                        elif is_first_day_pair_second:
                            log(f"🔒【月初保留保護】{d} の {p}さん({member_names.get(p, p)}) の『{orig_t}』は前月連動なしのため固定化されました。")
                            
                        for t in all_tasks:
                            if t != orig_t:
                                model.Add(x[p, d, t] == 0)
                        model.Add(x[p, d, orig_t] == 1)

            for p in existing_members:
                p_role = member_role.get(p, '')
                for d in dates:
                    if day_lock_flags.get(d, False):
                        continue
                    for t in all_tasks:
                        if not is_trade_allowed(t):
                            continue
                        if p_role == 'M' and t.endswith('C'):
                            model.Add(x[p, d, t] == 0)
                        elif p_role == 'C' and t.endswith('M'):
                            model.Add(x[p, d, t] == 0)

            for p in existing_members:
                p_gender = member_gender.get(p, '')
                if p_gender == 'F':
                    for d in dates:
                        if day_lock_flags.get(d, False):
                            continue
                        for t in all_tasks:
                            if not is_trade_allowed(t):
                                continue
                            if not is_female_allowed(t):
                                model.Add(x[p, d, t] == 0)

            for d in dates:
                if day_lock_flags.get(d, False):
                    continue
                tasks_today = [initial_assignment.get((p, d), '公休') for p in existing_members]
                for t in all_tasks:
                    if not is_trade_allowed(t):
                        continue
                    required_count = tasks_today.count(t)
                    model.Add(sum(x[p, d, t] for p in existing_members) == required_count)

            for d in dates:
                if day_lock_flags.get(d, False):
                    continue
                
                pair_swaps = {}
                for i in range(len(existing_members)):
                    for j in range(i + 1, len(existing_members)):
                        p1, p2 = existing_members[i], existing_members[j]
                        orig1 = initial_assignment.get((p1, d), '公休')
                        orig2 = initial_assignment.get((p2, d), '公休')
                        
                        p1_boundary_pair = (d == last_date and orig1 in first_day_pair_tasks) or (d == first_date and orig1 in second_day_pair_tasks)
                        p2_boundary_pair = (d == last_date and orig2 in first_day_pair_tasks) or (d == first_date and orig2 in second_day_pair_tasks)

                        if orig1 != orig2 and is_trade_allowed(orig1) and is_trade_allowed(orig2) and not p1_boundary_pair and not p2_boundary_pair:
                            s_var = model.NewBoolVar(f'pair_swap_{p1}_{p2}_{d}')
                            model.AddMinEquality(s_var, [x[p1, d, orig2], x[p2, d, orig1]])
                            pair_swaps[(p1, p2)] = s_var

                for p in existing_members:
                    orig_t = initial_assignment.get((p, d), '公休')
                    is_boundary_pair = (d == last_date and orig_t in first_day_pair_tasks) or (d == first_date and orig_t in second_day_pair_tasks)

                    if not is_trade_allowed(orig_t) or is_boundary_pair:
                        continue
                    
                    p_swaps = []
                    for (p1, p2), s_var in pair_swaps.items():
                        if p == p1 or p == p2:
                            p_swaps.append(s_var)
                    
                    model.Add(sum(p_swaps) == 1 - x[p, d, orig_t])

            for d_idx in range(len(dates) - 1):
                d_curr = dates[d_idx]
                d_next = dates[d_idx + 1]

                for work_curr, work_next_required in pair_rules.items():
                    if work_curr in tasks_by_day[d_curr] and work_next_required in tasks_by_day[d_next]:
                        for p in existing_members:
                            model.Add(x[p, d_curr, work_curr] == x[p, d_next, work_next_required])

            for p in existing_members:
                p_base_area = member_base_area.get(p, 'ANY')
                if p_base_area != 'ANY':
                    for d in dates:
                        if day_lock_flags.get(d, False):
                            continue
                        orig_t = initial_assignment.get((p, d), '公休')
                        for t in all_tasks:
                            if not is_trade_allowed(t) or t == orig_t:
                                continue
                            t_area = get_task_area(t)
                            if t_area != 'ANY' and t_area != p_base_area:
                                model.Add(x[p, d, t] == 0)

            objective_terms = []
            triple_rules = []
            for t1, t2 in pair_rules.items():
                if t2 in pair_rules:
                    t3 = pair_rules[t2]
                    if t1 in all_tasks and t2 in all_tasks and t3 in all_tasks:
                        triple_rules.append((t1, t2, t3))

            triple_trade_vars = []

            if len(dates) >= 3 and triple_rules:
                for d_idx in range(len(dates) - 2):
                    d1, d2, d3 = dates[d_idx], dates[d_idx + 1], dates[d_idx + 2]

                    if day_lock_flags.get(d1) or day_lock_flags.get(d2) or day_lock_flags.get(d3):
                        continue

                    for p1_idx in range(len(existing_members)):
                        for p2_idx in range(p1_idx + 1, len(existing_members)):
                            p1 = existing_members[p1_idx]
                            p2 = existing_members[p2_idx]

                            p1_t1, p1_t2, p1_t3 = initial_assignment.get((p1, d1)), initial_assignment.get((p1, d2)), initial_assignment.get((p1, d3))
                            p2_t1, p2_t2, p2_t3 = initial_assignment.get((p2, d1)), initial_assignment.get((p2, d2)), initial_assignment.get((p2, d3))

                            p1_is_triple = (p1_t1, p1_t2, p1_t3) in triple_rules and is_trade_allowed(p1_t1) and is_trade_allowed(p1_t2) and is_trade_allowed(p1_t3)
                            p2_is_triple = (p2_t1, p2_t2, p2_t3) in triple_rules and is_trade_allowed(p2_t1) and is_trade_allowed(p2_t2) and is_trade_allowed(p2_t3)

                            if p1_is_triple and p2_is_triple:
                                triple_swap_var = model.NewBoolVar(f'triple_swap_{p1}_{p2}_{d1}')
                                conds = [
                                    x[p1, d1, p2_t1], x[p1, d2, p2_t2], x[p1, d3, p2_t3],
                                    x[p2, d1, p1_t1], x[p2, d2, p1_t2], x[p2, d3, p1_t3]
                                ]
                                model.AddMinEquality(triple_swap_var, conds)
                                objective_terms.append(triple_swap_var * -100000)
                                triple_trade_vars.append((p1, p2, d1, d2, d3, (p1_t1, p1_t2, p1_t3), (p2_t1, p2_t2, p2_t3), triple_swap_var))

            LATE_EARLY_PENALTY_WEIGHT = 20

            for p in existing_members:
                for d_idx in range(len(dates) - 1):
                    d_curr = dates[d_idx]
                    d_next = dates[d_idx + 1]

                    for t_curr in all_tasks:
                        if task_end_type.get(t_curr) == 'LATE':
                            for t_next in all_tasks:
                                if task_start_type.get(t_next) == 'EARLY':
                                    late_early_var = model.NewBoolVar(f'late_early_{p}_{d_curr}_{t_curr}_{t_next}')
                                    model.AddMinEquality(late_early_var, [x[p, d_curr, t_curr], x[p, d_next, t_next]])
                                    objective_terms.append(late_early_var * LATE_EARLY_PENALTY_WEIGHT)

            member_overflow_vars = {}
            for p in existing_members:
                p_base_area = member_base_area.get(p, 'ANY')
                p_of_terms = []
                
                if p_base_area != 'ANY':
                    for d in dates:
                        if day_lock_flags.get(d, False):
                            continue
                        for t in all_tasks:
                            if not is_trade_allowed(t):
                                continue
                            t_area = get_task_area(t)
                            if t_area != 'ANY' and t_area != p_base_area:
                                p_of_terms.append(x[p, d, t])
                
                of_var = model.NewIntVar(0, len(dates), f'overflow_{p}')
                model.Add(of_var == sum(p_of_terms))
                member_overflow_vars[p] = of_var

                past_of = member_past_overflow.get(p, 0)
                weighted_penalty = 1000 + (past_of * 500)
                objective_terms.append(of_var * weighted_penalty)

            max_overflow_var = model.NewIntVar(0, len(dates), 'max_overflow')
            for p in existing_members:
                model.Add(max_overflow_var >= member_overflow_vars[p])
            
            objective_terms.append(max_overflow_var * 5000)

            for p in existing_members:
                for d in dates:
                    if day_lock_flags.get(d, False):
                        continue
                    orig_t = initial_assignment.get((p, d), '公休')
                    for t in all_tasks:
                        if is_trade_allowed(t) and t != orig_t:
                            objective_terms.append(x[p, d, t] * 1)

            model.Minimize(sum(objective_terms))

            log("ソルバーを実行中...")
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = 60.0
            status = solver.Solve(model)
            status_name = solver.StatusName(status)
            log(f"ソルバー実行完了 Status: {status_name}")

            change_logs = []
            triple_applied_logs = []
            pair_applied_logs = []
            changed_cells = set()
            overflow_cells = set()

            if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
                final_schedule = {}
                for d in dates:
                    for p in existing_members:
                        for t in all_tasks:
                            if solver.Value(x[p, d, t]) == 1:
                                final_schedule[(p, d)] = t
                                orig_t = initial_assignment.get((p, d), '公休')
                                if t != orig_t:
                                    p_name = member_names.get(p, p)
                                    change_logs.append(f"【{d}】{p_name}さん({p}) : {orig_t} ➔ {t}")
                                    changed_cells.add((p, d))
                                break

                for p1, p2, d1, d2, d3, (p1_t1, p1_t2, p1_t3), (p2_t1, p2_t2, p2_t3), svar in triple_trade_vars:
                    if solver.Value(svar) == 1:
                        name1 = member_names.get(p1, p1)
                        name2 = member_names.get(p2, p2)
                        triple_applied_logs.append(
                            f"【3連番一括トレード成立】{d1}〜{d3} : {name1}さん ({p1_t1}->{p1_t2}->{p1_t3}) 🔁 {name2}さん ({p2_t1}->{p2_t2}->{p2_t3})"
                        )

                result_rows = []
                
                if not daytype_row.empty:
                    r_dict = daytype_row.iloc[0].to_dict()
                    r_dict['OverFlow'] = ''
                    r_dict['3M_Total_OF'] = ''
                    result_rows.append(r_dict)
                if not lock_row.empty:
                    r_dict = lock_row.iloc[0].to_dict()
                    r_dict['OverFlow'] = ''
                    r_dict['3M_Total_OF'] = ''
                    result_rows.append(r_dict)

                for p in existing_members:
                    p_base_area = member_base_area.get(p, 'ANY')
                    overflow_count = 0
                    
                    row_src = df_initial_indexed.loc[p]
                    row = {
                        id_col_name: p,
                        name_col_name: member_names.get(p, '')
                    }

                    if col_m1:
                        row[col_m1] = parse_int_safely(row_src.get(col_m1, 0))
                    if col_m2:
                        row[col_m2] = parse_int_safely(row_src.get(col_m2, 0))

                    for d in dates:
                        task_assigned = final_schedule.get((p, d), initial_assignment.get((p, d), '公休'))
                        row[d] = task_assigned
                        
                        if not day_lock_flags.get(d, False) and is_trade_allowed(task_assigned):
                            t_area = get_task_area(task_assigned)
                            if p_base_area != 'ANY' and t_area != 'ANY' and p_base_area != t_area:
                                overflow_count += 1
                                overflow_cells.add((p, d))

                    row['OverFlow'] = int(overflow_count)
                    
                    past_of = member_past_overflow.get(p, 0)
                    row['3M_Total_OF'] = int(past_of + overflow_count)

                    result_rows.append(row)

                df_result = pd.DataFrame(result_rows)

                for m_col in [col_m1, col_m2]:
                    if m_col and m_col in df_result.columns:
                        df_result[m_col] = df_result[m_col].apply(
                            lambda v: int(float(v)) if pd.notna(v) and str(v).strip() != '' and str(v).replace('.','',1).isdigit() else ''
                        )

                for d_idx in range(len(dates) - 1):
                    d_curr = dates[d_idx]
                    d_next = dates[d_idx + 1]
                    for p in existing_members:
                        work_curr = final_schedule.get((p, d_curr), '公休')
                        if work_curr in pair_rules:
                            work_next = pair_rules[work_curr]
                            p_name = member_names.get(p, p)
                            pair_applied_logs.append(
                                f"【ペア整合確認】{p_name}さん({p}): {d_curr}『{work_curr}』 ➔ {d_next}『{work_next}』(完全連動)"
                            )
                
                return df_result, True, "OK", change_logs, pair_applied_logs, triple_applied_logs, changed_cells, overflow_cells, id_col_name, day_lock_flags, debug_logs
            else:
                return df_initial_raw, False, f"Solver Status: {status_name}", [], [], [], set(), set(), id_col_name, {}, debug_logs

        except Exception as e:
            err_msg = traceback.format_exc()
            log("❌ プログラム実行中に予期せぬエラーが発生しました:")
            log(err_msg)
            return df_initial_raw, False, f"Exception: {str(e)}", [], [], [], set(), set(), "", {}, debug_logs