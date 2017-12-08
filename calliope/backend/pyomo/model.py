"""
Copyright (C) 2013-2017 Calliope contributors listed in AUTHORS.
Licensed under the Apache 2.0 License (see LICENSE file).

"""

import os
import json
from contextlib import redirect_stdout, redirect_stderr

import numpy as np
import pandas as pd
import xarray as xr

import pyomo.core as po  # pylint: disable=import-error
from pyomo.opt import SolverFactory  # pylint: disable=import-error

# pyomo.environ is needed for pyomo solver plugins
import pyomo.environ  # pylint: disable=unused-import,import-error

# TempfileManager is required to set log directory
from pyutilib.services import TempfileManager  # pylint: disable=import-error

from calliope.backend.pyomo.util import get_var
from calliope.core.util.tools import load_function, LogWriter
from calliope.core.util.dataset import reorganise_dataset_dimensions
from calliope import exceptions


def generate_model(model_data):
    """
    Generate a Pyomo model.

    """
    backend_model = po.ConcreteModel()

    # Sets
    for coord in list(model_data.coords):
        set_data = list(model_data.coords[coord].data)
        # Ensure that time steps are pandas.Timestamp objects
        if isinstance(set_data[0], np.datetime64):
            set_data = pd.to_datetime(set_data)
        setattr(
            backend_model, coord,
            po.Set(initialize=set_data, ordered=True)
        )

    # "Parameters"
    model_data_dict = {
        'data': {k:
            model_data[k].to_series().dropna().replace('inf', np.inf).to_dict()
            for k in model_data.data_vars},
        'dims': {k: model_data[k].dims for k in model_data.data_vars},
        'sets': list(model_data.coords)
    }
    # Dims in the dict's keys are ordered as in model_data, which is enforced
    # in model_data generation such that timesteps are always last and the
    # remainder of dims are in alphabetic order
    backend_model.__calliope_model_data__ = model_data_dict
    backend_model.__calliope_defaults__ = json.loads(model_data.attrs['defaults'])

    # Variables
    load_function(
        'calliope.backend.pyomo.variables.initialize_decision_variables'
    )(backend_model)

    # Constraints
    constraints_to_add = [
        'energy_balance.load_energy_balance_constraints',
        'capacity.load_capacity_constraints',
        'dispatch.load_dispatch_constraints',
        'network.load_network_constraints',
        'costs.load_cost_constraints'
    ]

    for c in constraints_to_add:
        load_function(
            'calliope.backend.pyomo.constraints.' + c
        )(backend_model)

    # FIXME: Optional constraints
    # optional_constraints = model_data.attrs['constraints']
    # if optional_constraints:
    #     for c in optional_constraints:
    #         self.add_constraint(load_function(c))

    # Objective function
    objective_name = model_data.attrs['model.objective']
    objective_function = 'calliope.backend.pyomo.objective.' + objective_name
    load_function(objective_function)(backend_model)

    # delattr(backend_model, '__calliope_model_data__')

    return backend_model


def solve_model(backend_model, solver,
                solver_io=None, solver_options=None, save_logs=False):

    opt = SolverFactory(solver, solver_io=solver_io)

    if solver_options:
        for k, v in solver_options.items():
            opt.options[k] = v

    if save_logs:
        solve_kwargs = {
            'symbolic_solver_labels': True,
            'keepfiles': True
        }
        os.makedirs(save_logs, exist_ok=True)
        TempfileManager.tempdir = save_logs  # Sets log output dir
    else:
        solve_kwargs = {}

    with redirect_stdout(LogWriter('info', strip=True)):
        with redirect_stderr(LogWriter('error', strip=True)):
            results = opt.solve(backend_model, tee=True, **solve_kwargs)

    return results


def load_results(backend_model, results):
    """Load results into model instance for access via model variables."""
    not_optimal = (
        results['Solver'][0]['Termination condition'].key != 'optimal'
    )
    this_result = backend_model.solutions.load_from(results)

    # FIXME -- what to do here?
    # if this_result is False or not_optimal:
    #     # logging.critical('Solver output:\n{}'.format('\n'.join(self.pyomo_output)))
    #     # logging.critical(results.Problem)
    #     # logging.critical(results.Solver)
    #     if not_optimal:
    #         message = 'Model solution was non-optimal.'
    #     else:
    #         message = 'Could not load results into model instance.'
    #     raise exceptions.BackendError(message)


def get_result_array(backend_model):
    all_variables = {
        i.name: get_var(backend_model, i.name) for i in backend_model.component_objects()
        if isinstance(i, po.base.var.IndexedVar)
    }
    return reorganise_dataset_dimensions(xr.Dataset(all_variables))