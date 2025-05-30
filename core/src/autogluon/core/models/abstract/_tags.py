_DEFAULT_TAGS = {
    # [Advanced] Whether the model can support fitting on 100% of the data and then getting unbiased predictions on the same data.
    # it fit on by exploiting special properties of the model architecture.
    # For example, random forest uses only a portion of the training data randomly for each decision tree.
    # We can therefore use the out-of-bag predictions to obtain unbiased predictions.
    # Note that models that specify this as True must implement a `predict_proba_oof` method.
    # Refer to RandomForestModel or KNeighborsModel for reference implementations.
    "valid_oof": False,

    # Whether the model can be refit using the combined train and val data as training and no validation data without issue.
    #  TL;DR: Keep value as False unless you know what you are doing. This is advanced functionality.
    #  If False, when calling predictor.refit_full(), this model will simply be duplicated (if non-bag) or will have the first fold model duplicated (if bag).
    #  This will result in a slightly worse refit model than an optimally implemented refit_full, but is a simple fallback that is still effective.
    #  If True (Advanced), when calling predictor.refit_full(), this model will be fit again on 100% of the data as training data (no validation data)
    #  using hyperparameters defined by this model's implementation.
    #  If a model does not use validation data in any way during training, then it is safe to set `can_infer_full` to True without additional work.
    #  Some models use early stopping or more advanced techniques during training that require validation data.
    #  For these models, they should implement logic that communicates in the hyperparameters to the refit_full model
    #  the knowledge gained during the original fit.
    #  For example, if epochs=100 but the model early stopped on epoch=20 with epoch=10 having the best validation score,
    #  we want the refit_full model to stop training on epoch 10 (trusting that its performance will mimic the original model).
    #  This can be implemented via passing epoch=10 (best epoch) at end of `_fit` by setting (example): `self.params_trained['epochs'] = self.model.best_epoch`
    #  If the model has even more complex functionality associated with the `epochs` value itself (such as cyclic learning rate),
    #  a solution is to pass epochs=100 and
    #  implement a new parameter `final_epoch=10` that forces the model to stop at `final_epoch` (while maintaining the same LR schedule).
    #  This can get very complex to implement correctly for refit_full.
    #  It is recommended in these scenarios to set `can_refit_full` to False until a correct implementation is added.
    "can_refit_full": False,
}


_DEFAULT_CLASS_TAGS = {
    # Whether the model can handle raw text input features.
    #  Used for informing the global feature preprocessor on if it should keep raw text features.
    "handles_text": False,

    # Whether the model can estimate memory usage during fit without requiring initialization.
    # If True, can call `model.estimate_memory_usage_static(...)` to get a memory estimate.
    # For large datasets, it is much faster to get a memory estimate using this technique rather than having to first initialize the model
    # For example, going from 15s -> 0.1s, approximately a 100x speedup.
    "can_estimate_memory_usage_static": False,
}
