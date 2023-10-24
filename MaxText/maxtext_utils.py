def get_functional_train_full_signature(train_step, mesh, state_mesh_annotations, model, config):
  """ Get the shardings (both state and data) for train_step """
  functional_train = get_functional_train_step(train_step, model, config)
  data_pspec = P(*config.data_sharding)
  state_mesh_shardings = jax.tree_map(
      lambda p: jax.sharding.NamedSharding(mesh, p), state_mesh_annotations)
  data_sharding = jax.tree_map(
      lambda p: jax.sharding.NamedSharding(mesh, p), data_pspec)
  in_shardings = (state_mesh_shardings, data_sharding, None) # State, batch, rng
  out_shardings = (state_mesh_shardings, None, None) # State, metrics, rng
  static_argnums = () # We partial out the static argnums of model and config
  donate_argnums = 0 # This is the index of the state - we allow the compiler to make use of this memory.
  return functional_train, in_shardings, out_shardings, static_argnums, donate_argnums

def get_functional_train_step(train_step, model, config):
  # Modularized out so can be publicly called for xaot
  return functools.partial(train_step, model, config)

def get_optimizer(config, learning_rate_schedule):
  # We use AdamW following Llama2's training details, see https://arxiv.org/pdf/2307.09288.pdf section 2.2
  return optax.adamw(
    learning_rate_schedule,
    b1=config.adam_b1,
    b2=config.adam_b2,
    eps=config.adam_eps,
    eps_root=config.adam_eps_root,
    weight_decay=config.adam_weight_decay,
  )

  def validate_config(config):
    """ Validates the configuration is set correctly for train.py"""

    def _validate_gcs_bucket_name(bucket_name, config_var):
      assert bucket_name, f"Please set {config_var}."
      assert len(bucket_name) > 5 and bucket_name[0:5]=='gs://', f"Erroring out, {config_var} should start with 'gs://' "

    assert config.run_name, "Erroring out, need a real run_name"
    _validate_gcs_bucket_name(config.base_output_directory, "base_output_directory")
    _validate_gcs_bucket_name(config.dataset_path, "dataset_path")

    assert ((config.load_parameters_path=="" and config.load_from_other_directory=="") or
      config.enable_checkpointing), "You must set enable_checkpointing to load a checkpoint"
    assert config.load_parameters_path=="" or config.load_from_other_directory=="",\
    "At most one of load_parameters_path or load_from_other_directory should be set"
    assert config.load_from_other_directory_step==-1 or config.load_from_other_directory!="",\
    "You must specify the loading directory if you specify the loading step"
    assert config.steps > 0, "You must set steps or learning_rate_schedule_steps to a positive interger."

def load_compiled(config, partial_train, state):
  """ # Loading a serialized compiled train step function."""
  # Currently partial_train and state  are needed to reconstruct
  # input/output shapes to construct the in_trees and out_trees for load API
  # Parker is working on a serializing these
  def load_serialized_compiled(save_name):
    with open(save_name, "rb") as f:
      serialized_compiled = pickle.load(f)
    return serialized_compiled

  def get_io_trees(func, input_args, input_kwargs):
    _, in_tree_recreated = jax.tree_util.tree_flatten((input_args, input_kwargs))
    out_shaped = jax.eval_shape(func, *input_args, **input_kwargs)
    _, out_tree_recreated = jax.tree_util.tree_flatten(out_shaped)
    return in_tree_recreated, out_tree_recreated

  serialized_compiled = load_serialized_compiled(config.compiled_trainstep_file)
  shaped_batch = get_shaped_batch(config)
  example_rng = jax.random.PRNGKey(0)
  shaped_input_args = (state, shaped_batch, example_rng)
  shaped_input_kwargs = {}
  in_tree_recreated, out_tree_recreated = get_io_trees(partial_train, shaped_input_args, shaped_input_kwargs)
  p_train_step = deserialize_and_load(serialized_compiled, in_tree_recreated, out_tree_recreated)
  return p_train_step