import asyncio
import random
from typing import Callable, Coroutine

import pydantic

from ...langrt import JsonSerializable, LrtFunctionDef, LrtNode, LrtProgram, LrtSolution
from ...utils.logger import Console, ParaStatus
from ...utils.types import reshape
from ..shared import CodeGenContext, CodeGenJournal, CodeGenJournalist
from .defaults import DEFAULT_IMPORTS
from .gen_once import GenManySig
from .make_test import MakeTestSig, TestType


class RunnerCaseResult(pydantic.BaseModel):
    """Contains the result of each individual program * each evaluation."""

    test_type: TestType
    ok: bool
    result: JsonSerializable | None
    stdout: str | None
    duration: float
    pass


RunnerScoreSig = Callable[[list[list[RunnerCaseResult]]], list[float]]
"""Provides a score for each implementation based on the test results:
fn: (result[i][j] -> score[i])."""


async def funcoder_runner(
    ctx: CodeGenContext,
    opt_include_architect: bool,
    opt_samples: int,
    gen_pass: GenManySig,
    test_pass: MakeTestSig,
    score_pass: RunnerScoreSig,
    ancestors: list[LrtNode],
    func: LrtFunctionDef,
    descendants: list[LrtNode],
) -> tuple[tuple[LrtFunctionDef, list[LrtNode]] | None, CodeGenJournal]:
    """Sample one implementation based on `opt_samples` with provided test
    generator and scoring function."""

    ctx.log.in_scope(f"funcoder_runner[{func.name}]")
    ctx.log.object(
        dict[str, list[LrtNode]],
        {
            "ancestors": ancestors,
            "func": [func],
            "descendants": descendants,
        },
    )
    _sj = CodeGenJournalist(ctx, "runner", (ancestors, func, descendants))
    opt_retry_rate = 1.8
    opt_retries = int(opt_samples * opt_retry_rate)

    # step 1 of 4: populate subtree implementations
    impl_l: list[tuple[LrtFunctionDef, list[LrtNode]]] = []
    for _retry in range(1, opt_retries + 1):
        if len(impl_l) >= opt_samples:
            break
        impl_l, _sj_c = await gen_pass(ctx, ancestors, func, descendants, opt_samples)
        _sj.append(_sj_c)
        if impl_l:
            break
        ctx.log.string(f"attempt {_retry} failed: no program received")
    if not impl_l:
        ctx.log.warn(f"neither of the {opt_retries} attempts succeeded in generating code")
        # best-effort: try to generate at least one implementation even when fail
        return (func, descendants), _sj.collect_gen((func, descendants))
    if opt_include_architect:
        impl_l = [(func, descendants)] + impl_l

    # step 2 of 4: populate tests
    # if you need to custom how many tests of one kind and how many of another,
    # use pipelined test stages instead of hardcoding rules here
    tests, _sj_c = await test_pass(ctx, ancestors, [fn for fn, _ in impl_l])
    _sj.append(_sj_c)
    if not tests:
        ctx.log.warn(f"no tests available for self-consistency check")

    # step 3 of 4: run tests
    results = await runner_evaluate_cases(
        ctx=ctx,
        cfg_timeout=5.0,
        impls=[(LrtProgram(module=(), nodes=[*ancestors, fn, *rest, *descendants]), fn) for fn, rest in impl_l],
        tests=tests,
    )

    # step 4 of 4: judge results
    result_scores = score_pass(results)
    _max_score = max(result_scores)
    _max_indices = [i for i, score in enumerate(result_scores) if score == _max_score]
    result_verdict = random.choice(_max_indices)
    ctx.log.string(f"voted #{result_verdict} among: {result_scores}")

    result_final = impl_l[result_verdict]
    _log_code = LrtProgram(module=(), nodes=[result_final[0]] + result_final[1])
    ctx.log.code("python", f"voted implementation #{result_verdict}", ctx.lrt.pretty_fmt(_log_code))

    _log = _sj.collect_gen(
        result=result_final,
        _judge_results=results,
        _judge_scores=result_scores,
        _judge_verdict=result_verdict,
    )
    return result_final, _log


async def runner_evaluate_cases(
    ctx: CodeGenContext,
    cfg_timeout: float,
    impls: list[LrtFunctionDef]
    | list[tuple[LrtProgram, LrtFunctionDef]]
    | list[tuple[LrtSolution, LrtProgram, LrtFunctionDef]],
    tests: list[tuple[TestType, LrtProgram, LrtFunctionDef]],
) -> list[list[RunnerCaseResult]]:
    """(impl[i], test[j]) -> result[i][j].
    [TODO] The tests will be run under the same context as the entrypoint
    program so sufficient isolation measures must be taken."""

    # step 1 of 4: get canonical implementations
    real_impl: list[tuple[LrtSolution, LrtProgram, LrtFunctionDef]] = []
    for impl in impls:
        if isinstance(impl, tuple):
            if len(impl) == 3:
                real_impl.append(impl)
            elif len(impl) == 2:
                real_impl.append((LrtSolution(modules=[impl[0]]), impl[0], impl[1]))
        else:
            program = LrtProgram(module=(), nodes=[impl])
            real_impl.append((LrtSolution(modules=[program]), program, impl))

    # step 2 of 4: populate seeds for tests
    real_tests: list[tuple[TestType, LrtProgram, LrtFunctionDef, int]] = []
    for typ, test_prog, test_func in tests:
        seed = random.randint(0, 2**32 - 1)
        real_tests.append((typ, test_prog, test_func, seed))

    # step 3 of 4: run every test
    default_imports = ctx.lrt.parse((), DEFAULT_IMPORTS).nodes

    async def _run_case(
        _status: ParaStatus,
        _i: int,
        _j: int,
        impl: tuple[LrtSolution, LrtProgram, LrtFunctionDef],
        test: tuple[TestType, LrtProgram, LrtFunctionDef, int],
    ) -> RunnerCaseResult:
        # step 3.1. combine & prepare program-to-test
        impl_sln, impl_prog, impl_func = impl
        test_typ, test_prog, test_func, test_seed = test
        impl_sln = impl_sln.model_copy(deep=True)
        impl_prog = impl_sln.find(impl_prog)
        if not impl_prog:
            raise ValueError("cannot find program in solution")
        merge_prog = LrtProgram(module=impl_prog.module, nodes=[*default_imports, *impl_prog.nodes, *test_prog.nodes])
        merge_prog = ctx.lrt.prettify(merge_prog)
        merge_func = merge_prog.find(test_func)
        if not merge_func:
            raise ValueError("cannot find test function in program")
        merge_modules = [m for m in impl_sln.modules if m.module != impl_prog.module] + [merge_prog]
        merge_sln = LrtSolution(modules=merge_modules)

        # step 3.2. executing code
        #     with different seeds, the same test generator can create different
        #     results. we do not however run twice to ensure that the test
        #     generator since it is not mockable and that we cannot ensure the
        #     validity of either side of this.
        result = await ctx.lrt.run_solution(
            solution=merge_sln,
            from_module=merge_prog.module,
            func_name=merge_func.name,
            args=[test_seed],
            kwargs={},
            stdin="",
            timeout=cfg_timeout,
        )

        # step 3.3. print logs to the user
        _log = f"implementation #{_i + 1} : test #{_j + 1} : seed {test_seed}\n"
        _log += "-" * 32 + "\n"
        _log += result.model_dump_json(indent=2)
        ctx.log.exec_result(important=not result.ok, content=_log)
        _ok = "ok" if result.ok else result.error.split("\n")[-1]
        _status.update(f"[steel_blue1]running tests: implementation #{_i + 1} : test #{_j + 1} : {_ok}[/steel_blue1]")

        # translate
        return RunnerCaseResult(
            test_type=test_typ,
            ok=result.ok,
            result=result.result,
            stdout=result.stdout,
            duration=result.duration,
        )

    # step 4 of 4: schedule case runs
    results_lp: list[Coroutine[None, None, RunnerCaseResult]] = []
    with Console.get_status("[steel_blue1]running tests[/steel_blue1]", silent=ctx.cfg_silent) as _status:
        for _i, impl in enumerate(real_impl):
            for _j, test in enumerate(real_tests):
                results_lp.append(_run_case(_status, _i, _j, impl, test))
        results_l: list[RunnerCaseResult] = await asyncio.gather(*results_lp)
    results = reshape(results_l, (len(real_impl), len(real_tests)))
    return results
