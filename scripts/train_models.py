import multiprocessing
import pathlib
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple
import click

import click_pathlib

from npdependency import graph_parser
from npdependency import conll2018_eval as evaluator


class TrainResults(NamedTuple):
    dev_upos: float
    dev_las: float
    test_upos: float
    test_las: float


def train_single_model(
    train_file: pathlib.Path,
    dev_file: pathlib.Path,
    pred_file: pathlib.Path,
    out_dir: pathlib.Path,
    config_path: pathlib.Path,
    device: str,
    additional_args: Dict[str, str],
) -> TrainResults:
    graph_parser.main(
        [
            "--train_file",
            str(train_file),
            "--dev_file",
            str(dev_file),
            "--pred_file",
            str(pred_file),
            "--out_dir",
            str(out_dir),
            "--device",
            device,
            *(a for key, value in additional_args.items() for a in (f"--{key}", value)),
            str(config_path),
        ],
    )

    gold_devset = evaluator.load_conllu_file(dev_file)
    syst_devset = evaluator.load_conllu_file(out_dir / f"{dev_file.name}.parsed")
    dev_metrics = evaluator.evaluate(gold_devset, syst_devset)

    gold_testset = evaluator.load_conllu_file(pred_file)
    syst_testset = evaluator.load_conllu_file(out_dir / f"{pred_file.name}.parsed")
    test_metrics = evaluator.evaluate(gold_testset, syst_testset)

    return TrainResults(
        dev_upos=dev_metrics["UPOS"].f1,
        dev_las=dev_metrics["LAS"].f1,
        test_upos=test_metrics["UPOS"].f1,
        test_las=test_metrics["LAS"].f1,
    )


# It would be nice to be able to have this as a closure, but unfortunately it doesn't work since
# closures are not picklable and multiprocessing can only deal with picklable workers
def worker(device_queue, name, kwargs) -> Tuple[str, TrainResults]:
    # We use no more workers than devices so the queue should never be empty when spawning a
    # worker and we don't need to block here
    device = device_queue.get()
    kwargs["device"] = device
    print(f"Start training {name} on {device}")
    try:
        res = train_single_model(**kwargs)
    except Exception as e:
        raise Exception(e.message)
    device_queue.put(device)
    print(f"Run {name} finished with results {res}")
    return (name, res)


def run_multi(
    runs: Iterable[Tuple[str, Dict[str, Any]]], devices: List[str]
) -> List[Tuple[str, TrainResults]]:
    manager = multiprocessing.Manager()
    device_queue = manager.Queue()
    for d in devices:
        device_queue.put(d)

    with multiprocessing.Pool(len(devices)) as pool:
        try:
            res = pool.starmap(worker, ((device_queue, *r) for r in runs))
        except Exception as e:
            raise e

    return res


@click.command()
@click.argument(
    "configs_dir",
    type=click_pathlib.Path(resolve_path=True, exists=True, file_okay=False),
)
@click.argument(
    "treebanks_dir",
    type=click_pathlib.Path(resolve_path=True, exists=True, file_okay=False),
)
@click.option(
    "--out-dir",
    default=".",
    type=click_pathlib.Path(resolve_path=True, exists=False, file_okay=False),
)
@click.option(
    "--devices",
    "devices",
    default="cpu",
    callback=(lambda _ctx, _opt, val: val.split(",")),
    help="A comma-separated list of devices to run on.",
)
@click.option(
    "--fasttext-path",
    "fasttext",
    type=click_pathlib.Path(resolve_path=True, exists=True, dir_okay=False),
    help="The path to a pretrained FastText model",
)
def main(
    configs_dir: pathlib.Path,
    out_dir: pathlib.Path,
    treebanks_dir: pathlib.Path,
    devices: List[str],
    fasttext: Optional[pathlib.Path],
):
    out_dir.mkdir(parents=True, exist_ok=True)
    treebanks = [train.parent for train in treebanks_dir.glob("**/train.conllu")]
    configs = configs_dir.glob("*.yaml")
    additional_args = dict()
    if fasttext is not None:
        additional_args["fasttext"] = str(fasttext)
    runs: List[Tuple[str, Dict[str, Any]]] = []
    for c in configs:
        for t in treebanks:
            run_name = f"{t.name}-{c.stem}"
            runs.append(
                (
                    run_name,
                    {
                        "train_file": t / "train.conllu",
                        "dev_file": t / "dev.conllu",
                        "pred_file": t / "test.conllu",
                        "out_dir": out_dir / run_name,
                        "config_path": c,
                        "additional_args": additional_args,
                    },
                )
            )

    res = run_multi(runs, devices)

    with open(out_dir / "summary.tsv", "w") as out_stream:
        out_stream.write("run\tdev UPOS\tdev LAS\ttest UPOS\ttest LAS\n")
        for name, scores in res:
            out_stream.write(
                f"{name}\t{100*scores.dev_upos:.2f}\t{100*scores.dev_las:.2f}\t{100*scores.test_upos:.2f}\t{100*scores.test_las:.2f}\n"
            )


if __name__ == "__main__":
    main()