from __future__ import annotations

import json
from pathlib import Path

from datasets import load_dataset


DATASET = "SWE-bench/SWE-bench_Verified"
REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"
IMAGES = {
    "pydata__xarray-6992": "swebench/sweb.eval.x86_64.pydata_1776_xarray-6992@sha256:a70eb8c7e98b984c8c9a75c8e165d564d2f0bd16c1717320dbab40f771691112",
    "sphinx-doc__sphinx-7590": "swebench/sweb.eval.x86_64.sphinx-doc_1776_sphinx-7590@sha256:5c86dab7120b83b3bdb4af86743a3910da328029ed92183477fe9865ea76cb2f",
    "sympy__sympy-13878": "swebench/sweb.eval.x86_64.sympy_1776_sympy-13878@sha256:d0796de4f7c4749e36e32b043edc2c98b3c6f7127691af37c325944726aa7d0a",
    "astropy__astropy-13398": "swebench/sweb.eval.x86_64.astropy_1776_astropy-13398@sha256:49cfb8e47c492fac9570ee66f8d5e4a7aa2a2516e18cf5663aa73e5b352b670a",
    "django__django-16560": "swebench/sweb.eval.x86_64.django_1776_django-16560@sha256:42b251fee6107a5c2ba27ed14afb485f635c6a6118dae91cf505fd05337e0181",
    "pylint-dev__pylint-4551": "swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-4551@sha256:910cb070fc5e1c02b715b561c2f5783bb5f5e5935d9a169b4c91bdcfdd015f52",
    "pytest-dev__pytest-5787": "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-5787@sha256:48c6ca4406eb36dae43e08dc2137b750239b8ad8f9ccdf2924e8fc2177fca241",
    "scikit-learn__scikit-learn-25102": "swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-25102@sha256:a867569ffda421d052245801f98064f13460291d06f439b2e6a05ab7c3135d57",
}


def main() -> None:
    dataset = load_dataset(DATASET, split="test", revision=REVISION)
    rows = {row["instance_id"]: dict(row) for row in dataset}
    selected = []
    for instance_id, image in IMAGES.items():
        row = rows[instance_id]
        row["image"] = image
        selected.append(row)
    destination = Path(__file__).with_name("tasks.json")
    destination.write_text(json.dumps(selected, indent=2) + "\n")


if __name__ == "__main__":
    main()
