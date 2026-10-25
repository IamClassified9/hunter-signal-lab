
import numpy as np
from tabpfn_client import TabPFNClassifier

# Synthetic integration-test data only.
# These labels are NOT real trading outcomes.
X_train = np.array([
    [3.8, 1.2, 0.9],
    [2.1, 0.4, 0.3],
    [1.4, 0.8, 0.5],
    [3.2, 1.1, 0.8],
    [0.9, 0.2, 0.1],
    [1.8, 0.6, 0.4],
    [4.0, 1.3, 0.9],
    [1.1, 0.3, 0.2],
], dtype=np.float64)

y_train = np.array([
    "continuation",
    "reversal",
    "sideways",
    "continuation",
    "reversal",
    "sideways",
    "continuation",
    "reversal",
])

X_test = np.array([[2.5, 0.8, 0.6]], dtype=np.float64)

def main():
    print("Initializing TabPFN hosted inference test...")

    model = TabPFNClassifier()
    model.fit(X_train, y_train)

    probabilities = model.predict_proba(X_test)

    print("\nClasses:", model.classes_)
    print("Probabilities:", probabilities[0])
    print("\nTabPFN hosted inference succeeded.")
    print("WARNING: Synthetic test results are not trading predictions.")

if __name__ == "__main__":
    main()
