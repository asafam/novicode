from typing import List, Union, Optional, TypeVar, Generic
import os
import pandas as pd
import numpy as np
import ast
import math
import glob
from representations.tree.tree import Tree
from representations.builders.ast.tearers.tearer_factory import TearerFactory
import tokenize
from nltk.translate import bleu_score
from nltk.translate.bleu_score import SmoothingFunction
from sklearn import metrics
from tqdm.auto import tqdm


def parse_code_rep_to_code(code_rep: str, verbose: str = "Fatal") -> str:
    try:
        tree = Tree.unparse(code_rep)
        tearer = TearerFactory().get_tearer(tree.root_node)
        asdl = tearer.tear(tree.root_node)
        code = ast.unparse(asdl)
    except Exception as e:
        if verbose == "Error":
            print(f"[Error] failed to prase code rep to code:\n", e)
        code = ""
    finally:
        return code


def build_test_code(
    code: str,
    imports: str,
    test: str,
    code_embed_str: str = "# end code block to test",
    fail_on_error: bool = False,
    verbose: str = "Fatal",
):
    try:
        code_insert_idx = test.find(code_embed_str)
        program_code = imports
        program_code += "\n"
        program_code += test[:code_insert_idx]
        program_code += code
        program_code += "\n"
        program_code += test[code_insert_idx:]
    except Exception as e:
        if verbose == "Error":
            print("[ERROR] Failed to unparse code rep to code\n", e)
        if fail_on_error:
            raise e
        program_code = ""
    finally:
        return program_code


def tokenize_source(code):
    file_path = "/tmp/example.py"

    with open(file_path, "w") as text_file:
        text_file.write(code)

    with open(file_path, "rb") as f:
        tokens_gen = tokenize.tokenize(f.readline)

        tokens = [token.string for token in tokens_gen]

    os.remove(file_path)
    return tokens


def eval_code(code: str):
    test_results = {}
    try:
        context = {}
        exec(code, context)
        test_results = context.get("test_results", {})
        test_results["execution_success"] = test_results.get("execution_success", 0) + 1
    except AssertionError as e:
        test_results["assertion_failure"] = test_results.get("assertion_failure", 0) + 1
    except Exception as e:
        test_results["execution_failure"] = test_results.get("execution_failure", 0) + 1

    code_failure = test_results.get("code_failure", 0)
    assertion_failure = test_results.get("assertion_failure", 0)
    execution_failure = test_results.get("execution_failure", 0)
    execution_success = test_results.get("execution_success", 0)
    correct = test_results.get("correct", 0)
    incorrect = test_results.get("incorrect", 0)
    total = (correct + incorrect) or math.inf
    accuracy = (1 - code_failure) * (correct / total)

    results = dict(
        code_failure=code_failure,
        execution_success=execution_success,
        execution_failure=execution_failure,
        assertion_failure=assertion_failure,
        correct=correct,
        incorrect=incorrect,
        accuracy=accuracy,
    )

    return results


def generate_predictions(
    model, tokenizer, dataloader, gold_column, id_labels, max_length, k
):
    model.eval()
    outputs = []
    targets = []
    ks = []
    ids = {}
    for id_label in id_labels:
        ids[id_label] = []

    for batch in tqdm(dataloader):
        outs = model.generate(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
            max_length=max_length,
            do_sample=k > 1,
            num_return_sequences=k,
        )

        output = [tokenizer.decode(out, skip_special_tokens=True) for out in outs]
        target = [t.strip() for t in list(np.repeat(batch[gold_column], k))]

        outputs.extend(output)
        targets.extend(target)
        ks.extend(list(np.arange(k)) * (batch["input_ids"].shape[0]))
        for id_label in id_labels:
            ids[id_label].extend(list(np.repeat(batch[id_label], k)))

    data = pd.DataFrame(
        {
            **{
                "output": outputs,
                "target": targets,
                "k": ks,
            },
            **ids,
        }
    )

    return data


def eval_model(data: pd.DataFrame):
    results = dict(
        exact=metrics.accuracy_score(data["target"], data["output"]),
        bleu=None,
        humaneval=eval_model_humaneval(data["target"], data["output"]),
    )

    return results


def humaneval_accuracy_score(
    data: pd.DataFrame,
    code_column_name: str = "pred_code",
    score_id_labels1: Union[str, List[str]] = ["sample_id", "k"],
    score_id_labels2: Union[str, List[str]] = "sample_id",
    score_column_name: str = "accuracy",
):
    test_codes = data.apply(
        lambda x: build_test_code(
            code=x[code_column_name], imports=x["imports"], test=x["test"]
        ),
        axis=1,
    )
    test_results = test_codes.apply(lambda test_code: eval_code(test_code))
    test_results_df = pd.DataFrame.from_records(
        test_results.values, index=test_results.index
    )
    test_scores = (
        test_results_df.reset_index(drop=False)
        .groupby(score_id_labels1)[score_column_name]
        .mean()
    )
    score = (
        test_scores.reset_index(drop=False)
        .groupby(score_id_labels2)[score_column_name]
        .max()
        .mean()
    )
    return dict(score=score, results=test_results_df)


def bleu_accuracy_score(
    data: pd.DataFrame,
    generated_column="output",
    gold_column="code",
    score_id_labels1: Union[str, List[str]] = ["sample_id", "k"],
    score_id_labels2: Union[str, List[str]] = "sample_id",
    score_column_name: str = "bleu_score",
):
    eval_results = data.apply(
        lambda x: eval_bleu(x[gold_column], x[generated_column]), axis=1
    )
    eval_results_df = eval_results.to_frame("bleu_score")
    test_scores = (
        eval_results_df.reset_index(drop=False)
        .groupby(score_id_labels1)[score_column_name]
        .mean()
    )
    score = (
        test_scores.reset_index(drop=False)
        .groupby(score_id_labels2)[score_column_name]
        .max()
        .mean()
    )
    return dict(score=score, results=eval_results_df)


def model_eval(
    results_df=None,
    results_file_path=None,
    output_column="output",
    gold_column="code",
    parse_to_code=False,
    compute_humanval=True,
    compute_bleu=True,
):
    results_df = (
        pd.read_csv(results_file_path) if results_file_path else results_df.copy()
    )
    results_df["sample_id"] = results_df["sample_id"].astype(int)
    results_df.set_index(["sample_id", "sample_minor_id", "k"], inplace=True)
    results_df.sort_index(inplace=True)

    code_column = "generated_code"
    if parse_to_code:
        results_df[code_column] = results_df[output_column].apply(
            lambda x: parse_code_rep_to_code(x)
        )
    else:
        results_df[code_column] = results_df[output_column]

    results_df["test"] = results_df["test"].str.replace(
        "= next(iterator)", "= next(iterator, None)"
    )
    results_df[code_column] = results_df[code_column].str.replace(
        " = ContentType.", " = MessageContentType."
    )
    results_df[code_column] = results_df[code_column].str.replace(
        "Message.", "Messages."
    )

    humaneval_results = (
        humaneval_accuracy_score(data=results_df, code_column_name=code_column)
        if compute_humanval
        else {}
    )

    bleu_results = (
        bleu_accuracy_score(
            data=results_df, generated_column=code_column, gold_column=gold_column
        )
        if compute_bleu
        else {}
    )

    results = dict(humaneval=humaneval_results, bleu=bleu_results)
    return results


def eval_model_humaneval(
    data: pd.DataFrame,
    code_column_name: str = "pred_code",
    score_id_labels: Union[str, List[str]] = "sample_id",
    score_column_name: str = "accuracy",
):
    test_codes = data.apply(
        lambda x: build_test_code(
            code=x[code_column_name], imports=x["imports"], test=x["test"]
        ),
        axis=1,
    )
    test_results = test_codes.apply(lambda test_code: eval_code(test_code))

    test_results_df = pd.DataFrame.from_records(
        test_results.values, index=test_results.index
    )
    score = (
        test_results_df.reset_index(drop=False)
        .groupby(score_id_labels)[score_column_name]
        .mean()
        .mean()
    )
    return score, test_results_df


def eval_bleu(code, generated_code):
    if not code or not generated_code:
        return 0

    hypothesis = tokenize_source(code)

    try:
        reference = tokenize_source(generated_code)
    except:
        return 0

    n = max(min(len(hypothesis), 4), 1)
    weight = 1 / n
    weights = (weight,) * n
    smoothing_function = SmoothingFunction().method4
    score = bleu_score.sentence_bleu(
        [reference], hypothesis, weights=weights, smoothing_function=smoothing_function
    )
    return score


def eval_generated_code(
    df,
    model,
    tokenizer,
    k,
    dataloader,
    target_label,
    id_labels,
    max_length,
    output_column="output",
    gold_column="code",
    parse_code=False,
    file_path=None,
    should_generate_predictions=True,
    should_model_eval=True,
):
    # check if file exists
    file_exists = file_path and os.path.exists(file_path)

    if should_generate_predictions and not file_exists:
        preds_df = generate_predictions(
            model,
            tokenizer,
            dataloader=dataloader,
            gold_column=target_label,
            id_labels=id_labels,
            max_length=max_length,
            k=k,
        )

        if file_path:
            results_df = df.join(preds_df.set_index(df.index.names))
            results_df.to_csv(file_path)
            print(f"Results were saved to {file_path}")
    else:
        results_df = pd.read_csv(file_path)

    results = None
    if should_model_eval:
        results = model_eval(
            results_df=results_df,
            parse_to_code=parse_code,
            compute_humanval=True,
            compute_bleu=True,
            output_column=output_column,
            gold_column=gold_column,
        )
        print(f"humaneval = {results['humaneval']['score']}")
        print(f"bleu = {results['bleu']['score']}")

    return results
