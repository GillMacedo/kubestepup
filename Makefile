.PHONY: test lint demo

test:
	python3 -m unittest discover -s tests

lint:
	claude plugin validate ./plugin --strict
	claude plugin validate . --strict

# Reads made-up data through the stand-in kubectl in tests/fakes. Exit 1 means it found blockers, which it does on purpose.
demo:
	PATH="$$PWD/tests/fakes:$$PATH" python3 plugin/scripts/kubestepup.py --to 1.33 --kube --manifests examples/manifests || test $$? -eq 1
