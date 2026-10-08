# دليل حدود التنفيذ

**مُولَّد — لا يُحرَّر يدويًا.** أنشأه `scripts/record_boundary_proof.py` في 2026-10-08 14:57 UTC،
على الشيفرة `a81b50ddc` («feat(boundary): add a recorder that refuses to write a proof it cannot produce»)، وشجرة العمل عند التسجيل نظيفة عدا هذا الملف نفسه (وهو ما يُكتب الآن).

## ما هذا الملف، وما ليس هو

`ci.yml` يطبع تعليقًا على كل تشغيل يقول إن اختبارات التنفيذ **تُتخطّى** على عدّاء
GitHub المشترك، فالتشغيل هناك يُثبت مسار الرفض ولا يُثبت الفرض. ذلك التعليق صادق،
وهو أيضًا فجوة قائمة: لم يكن في المستودع أي تشغيل بُني فيه الحدود فعلًا ثم هُوجم.

هذا الملف هو ذلك التشغيل.

**وليس** هو: ليس جزءًا من CI، ولا يُغني عن `boundary-proof.yml` (الذي ما زال ملفًا
لم يُشغَّل، ويحتاج عدّاءً مخصّصًا لا وجود له بعد)، ولا يُثبت أن الحدود تُبنى على كل
مضيف — بل على المضيف الموصوف أدناه وحده. وما زال `C25` عند `Partial`: سقف الموارد
الكلي بـcgroup ناقص كما هو.

## البيئة التي سُجّل عليها

| البند | القيمة |
|---|---|
| النواة | `Linux 6.1.158+ (x86_64)` |
| المستخدم | `uid 1001 (غير مميّز)` |
| بايثون | `3.11.2` |
| unshare | `unshare from util-linux 2.38.1` |
| قيد AppArmor على userns | `(غير موجود)` |
| max_user_namespaces | `15734` |
| cgroup | `v2` |
| علامة حاوية | `لا` |

## الحدود التي بُنيت فعلًا

| البند | القيمة |
|---|---|
| العدّاء | `private-root` |
| القدرات المُعلنة | `cpu_cap`, `env_allowlist`, `file_size_cap`, `filesystem_isolation`, `memory_cap`, `no_network`, `output_cap`, `pid_isolation`, `process_cap`, `timeout` |
| الناقص | (لا شيء) |
| عزل نظام الملفات | نعم |

## النتيجة

| القياس | العدد |
|---|---|
| اختبارات مجموعة في `test_agent_sandbox.py` | `46` |
| **نُفّذت** | **`46`** |
| **تُخطّيت** | **`0`** |
| فشل / أخطاء | `0` / `0` |
| منها في `TheBoundaryHoldsAgainstRealCode` | `25` (كلها نُفّذت) |

صفر تخطٍّ هو الفرق بين هذا الملف وتعليق CI: هناك `21` اختبارًا
تُثبت الرفض، وهنا `46` تُثبت الفرض أيضًا.

## المقارنة مع عدّاء GitHub

مقيسة بإعادة تشغيل `detect()` بنص الرفض نفسه الذي ينقله التعليق، في مفسّر جديد:

- يُجمع الملف `46` اختبارًا.
- تنفيذ: `21`، وتُخطّي: **`25`**، وفشل: `0`.
- السبب المنقول: `unshare: write failed /proc/self/uid_map: Operation not permitted`.
- أي أن الحدود هناك **لم تُبنَ**، وهذا الملف يسجّل الـ`46` مقابلها.

## السجل الكامل

```text
test_a_refused_call_is_an_error_the_loop_can_record (test_agent_sandbox.CodeExecIsOffAndGated.test_a_refused_call_is_an_error_the_loop_can_record)
`runtime._run_tool` catches ToolError and stores it as data; that contract ... ok
test_code_exec_is_registered_but_disabled_by_default (test_agent_sandbox.CodeExecIsOffAndGated.test_code_exec_is_registered_but_disabled_by_default) ... ok
test_code_exec_must_be_approved_and_is_not_a_read_only_tool (test_agent_sandbox.CodeExecIsOffAndGated.test_code_exec_must_be_approved_and_is_not_a_read_only_tool) ... ok
test_the_prompt_only_advertises_it_when_turned_on (test_agent_sandbox.CodeExecIsOffAndGated.test_the_prompt_only_advertises_it_when_turned_on) ... ok
test_the_tool_refuses_with_a_reason_when_no_boundary_exists (test_agent_sandbox.CodeExecIsOffAndGated.test_the_tool_refuses_with_a_reason_when_no_boundary_exists)
Asserted by code, never by wording. A test that checks the sentence fails ... ok
test_a_runner_that_claims_the_capability_without_a_namespace_is_stopped (test_agent_sandbox.OneWorkspaceNotTwo.test_a_runner_that_claims_the_capability_without_a_namespace_is_stopped)
A safety net against a lying declaration, tested with a lying declaration. ... ok
test_a_workspace_outside_the_task_tree_never_reaches_the_boundary (test_agent_sandbox.OneWorkspaceNotTwo.test_a_workspace_outside_the_task_tree_never_reaches_the_boundary) ... ok
test_the_unavailable_runner_refuses_every_request_before_any_check_of_it (test_agent_sandbox.OneWorkspaceNotTwo.test_the_unavailable_runner_refuses_every_request_before_any_check_of_it)
The order of refusals is a contract: on a host with no boundary, nothing ... ok
test_a_failing_program_returns_data_instead_of_raising (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_a_failing_program_returns_data_instead_of_raising)
The loop must survive it: the exception is the program's, not the runner's. ... ok
test_a_grandchild_does_not_outlive_the_deadline (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_a_grandchild_does_not_outlive_the_deadline)
A kill that only reaps the direct child leaves a forked helper running. ... ok
test_a_program_that_never_ends_is_killed_at_the_deadline (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_a_program_that_never_ends_is_killed_at_the_deadline) ... ok
test_a_run_without_a_workspace_still_keeps_nothing (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_a_run_without_a_workspace_still_keeps_nothing)
The default did not change: no caller directory, nothing left behind. ... ok
test_a_successful_run_is_recognisable_as_one (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_a_successful_run_is_recognisable_as_one) ... ok
test_a_write_from_outside_is_the_same_file_inside (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_a_write_from_outside_is_the_same_file_inside)
The inode, in both directions, against the task's own directory. ... ok
test_a_written_file_cannot_exceed_the_size_cap (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_a_written_file_cannot_exceed_the_size_cap) ... ok
test_cpu_time_is_capped_even_with_the_clock_still_running (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_cpu_time_is_capped_even_with_the_clock_still_running) ... ok
test_host_paths_are_not_visible_at_all (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_host_paths_are_not_visible_at_all) ... ok
test_memory_is_capped_by_the_kernel (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_memory_is_capped_by_the_kernel) ... ok
test_nothing_of_the_run_survives_on_the_host (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_nothing_of_the_run_survives_on_the_host) ... ok
test_output_is_truncated_at_the_cap_and_says_so (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_output_is_truncated_at_the_cap_and_says_so) ... ok
test_simultaneous_processes_are_capped (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_simultaneous_processes_are_capped)
The cap that was advertised before it was tested. ... ok
test_stdout_and_stderr_are_reported_separately (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_stdout_and_stderr_are_reported_separately) ... ok
test_the_exit_status_is_the_programs_own (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_exit_status_is_the_programs_own) ... ok
test_the_network_is_denied_by_the_kernel_not_by_a_promise (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_network_is_denied_by_the_kernel_not_by_a_promise) ... ok
test_the_observation_is_json_serialisable_for_the_store (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_observation_is_json_serialisable_for_the_store)
`runtime._run_tool` json-dumps the handler's return value into ... ok
test_the_parent_environment_is_not_inherited_at_all (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_parent_environment_is_not_inherited_at_all) ... ok
test_the_process_tree_is_its_own_namespace (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_process_tree_is_its_own_namespace)
Counting files under `/proc` measures nothing -- most entries are kernel ... ok
test_the_runner_itself_refuses_before_it_spawns_anything (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_runner_itself_refuses_before_it_spawns_anything)
Same rule, now through the boundary: nothing is forked for a program that ... ok
test_the_runtime_is_read_only_inside_the_sandbox (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_runtime_is_read_only_inside_the_sandbox) ... ok
test_the_tool_uses_the_configured_limits (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_tool_uses_the_configured_limits)
The knobs are the ceilings the run actually receives. ... ok
test_the_workspace_is_writable_and_the_caller_keeps_the_result (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_the_workspace_is_writable_and_the_caller_keeps_the_result)
The other half of the contract: a private root that swallowed the ... ok
test_work_is_bounded_by_wall_clock_not_just_by_cpu (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_work_is_bounded_by_wall_clock_not_just_by_cpu)
A program that sleeps forever burns no CPU but must still be stopped. ... ok
test_writing_a_host_path_cannot_touch_the_host (test_agent_sandbox.TheBoundaryHoldsAgainstRealCode.test_writing_a_host_path_cannot_touch_the_host)
The finding that blocked R7.1. ... ok
test_a_network_only_boundary_is_not_enough_to_execute (test_agent_sandbox.TheBoundaryIsReportedNotAssumed.test_a_network_only_boundary_is_not_enough_to_execute)
The finding that produced R7.3. ... ok
test_an_aggregate_memory_bound_is_reported_not_implied (test_agent_sandbox.TheBoundaryIsReportedNotAssumed.test_an_aggregate_memory_bound_is_reported_not_implied)
`RLIMIT_AS` is per process; the honest total is the product. ... ok
test_every_advertised_capability_is_one_a_test_can_falsify (test_agent_sandbox.TheBoundaryIsReportedNotAssumed.test_every_advertised_capability_is_one_a_test_can_falsify)
A capability nobody tests is a claim nobody checked. ... ok
test_the_run_says_which_boundary_it_got (test_agent_sandbox.TheBoundaryIsReportedNotAssumed.test_the_run_says_which_boundary_it_got) ... ok
test_a_program_that_fits_the_cap_passes_the_check (test_agent_sandbox.TheRefusalPathFailsClosed.test_a_program_that_fits_the_cap_passes_the_check)
The guard must be a bound, not a wall: a program inside the cap is let ... ok
test_an_empty_program_is_refused_rather_than_run (test_agent_sandbox.TheRefusalPathFailsClosed.test_an_empty_program_is_refused_rather_than_run) ... ok
test_an_oversized_program_is_refused_by_name (test_agent_sandbox.TheRefusalPathFailsClosed.test_an_oversized_program_is_refused_by_name)
The rule is about the program, not the machine, so it is tested here -- ... ok
test_detect_does_not_decide_from_the_binary_being_present (test_agent_sandbox.TheRefusalPathFailsClosed.test_detect_does_not_decide_from_the_binary_being_present)
A machine can have `unshare` and still forbid user namespaces. ... ok
test_detect_returns_the_refusing_runner_when_the_binary_is_absent (test_agent_sandbox.TheRefusalPathFailsClosed.test_detect_returns_the_refusing_runner_when_the_binary_is_absent) ... ok
test_detect_returns_the_refusing_runner_when_the_probe_fails (test_agent_sandbox.TheRefusalPathFailsClosed.test_detect_returns_the_refusing_runner_when_the_probe_fails) ... ok
test_the_machine_refusal_wins_over_the_program_check (test_agent_sandbox.TheRefusalPathFailsClosed.test_the_machine_refusal_wins_over_the_program_check)
Deliberate precedence: with no boundary, nothing runs either way, so the ... ok
test_the_probe_result_is_cached_but_can_be_forced (test_agent_sandbox.TheRefusalPathFailsClosed.test_the_probe_result_is_cached_but_can_be_forced) ... ok
test_without_a_usable_boundary_nothing_runs (test_agent_sandbox.TheRefusalPathFailsClosed.test_without_a_usable_boundary_nothing_runs) ... ok

----------------------------------------------------------------------
Ran 46 tests in 9.308s

OK
```

## إعادة الإنتاج

```bash
python scripts/record_boundary_proof.py --out BOUNDARY-PROOF.md
```

على مضيف يمنع user namespaces يخرج السكربت بـ`2` **ولا يكتب ملفًا**:
دليل مكتوب حيث تُتخطّى الاختبارات هو نفس الكذبة التي يرفضها `ci.yml`.

## حدود هذا الدليل

- مضيف واحد ونواة واحدة (6.1.158+): يُثبت أن الاختبارات قابلة للتكذيب
  وأن الحدود تصمد هنا، ولا يقول شيئًا عن مضيف آخر.
- تاريخ التسجيل جزء من الدليل: القيد في النواة، والنواة تتغيّر.
- لا يُغلق سقف الموارد الكلي (`C25` يبقى `Partial`)، ولا يُشغّل فحص الطفرة.
