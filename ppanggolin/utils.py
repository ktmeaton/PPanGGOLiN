#!/usr/bin/env python3
# coding:utf-8

# default libraries
import sys
import os
import gzip
import argparse
from io import TextIOWrapper
from pathlib import Path
from typing import TextIO, Union, BinaryIO

import networkx as nx
import pkg_resources
from numpy import repeat
import logging
import tempfile
from collections.abc import Callable


from scipy.sparse import csc_matrix

import yaml
from collections import defaultdict


from ppanggolin.geneFamily import GeneFamily
# import ppanggolin.nem.rarefaction
# import ppanggolin.graph
# import ppanggolin.annotate
# import ppanggolin.cluster
# import ppanggolin.figures
# import ppanggolin.formats
# import ppanggolin.info
# import ppanggolin.metrics
# import ppanggolin.align
# import ppanggolin.RGP
# import ppanggolin.mod
# import ppanggolin.context
# import ppanggolin.workflow
# import ppanggolin.utility

# from ppanggolin import subcommand_to_subparser


# all input params that exists in ppanggolin
ALL_INPUT_PARAMS = ['fasta', 'anno', 'clusters', 'pangenome']

# all params that should be in the general_parameters section of the config file
ALL_GENERAL_PARAMS = ['output', 'basename', 'rarefaction', 'write_flat_output', 'tmpdir', 'verbose', 'log', 'disable_prog_bar', 'force']

WORKFLOW_SUBCOMMANDS = {'all', 'workflow', 'panrgp', 'panmodule'}

# command that can be launched inside a workflow subcommand
ALL_WORKFLOW_DEPENDENCIES = ["annotate", "cluster", "graph", "partition", "write", "rgp", "spot", "module" ]

# Inside a workflow command, write output default is overwrite to output some of the flat files
WRITE_FLAGS = ["csv", "Rtab", "gexf", "light_gexf",
                'projection', 'stats', 'json', 'partitions', 'regions', 'spots',
                'borders', 'modules', 'spot_modules']

# SUBCOMMAND_TO_SUBPARSER = {
#         "annotate":ppanggolin.annotate.subparser,
#         "cluster":ppanggolin.cluster.subparser,
#         "graph":ppanggolin.graph.subparser,
#         "partition":ppanggolin.nem.partition.subparser,
#         "rarefaction":ppanggolin.nem.rarefaction.subparser,
#         "workflow":ppanggolin.workflow.workflow.subparser,
#         "panrgp":ppanggolin.workflow.panRGP.subparser,
#         "panModule":ppanggolin.workflow.panModule.subparser,
#         "all":ppanggolin.workflow.all.subparser,
#         "draw":ppanggolin.figures.subparser,
#         "write":ppanggolin.formats.writeFlat.subparser,
#         "fasta":ppanggolin.formats.writeSequences.subparser,
#         "msa":ppanggolin.formats.writeMSA.subparser,
#         "metrics":ppanggolin.metrics.metrics.subparser,
#         "align":ppanggolin.align.subparser,
#         "rgp":ppanggolin.RGP.genomicIsland.subparser,
#         "spot":ppanggolin.RGP.spot.subparser,
#         "module":ppanggolin.mod.subparser,
#         "context":ppanggolin.context.subparser,
#         "info":ppanggolin.info.subparser,
#         "default_config":ppanggolin.utility.default_config.subparser }


def check_log(log_file: str) -> TextIO:
    """
    Check if the output log is writable

    :param name: Path to the log output

    :return: output for log
    """
    
    if log_file == "stdout":
        return sys.stdout
    elif log_file == "stderr":
        return sys.stderr
    
    elif os.path.exists(log_file):
        # path exists
        if os.path.isfile(log_file): # is it a file or a dir?
            # also works when file is a link and the target is writable
            if os.access(log_file, os.W_OK):
                return log_file
            else:
                raise IOError(f"The given log file {log_file} is not writable. Please check if it is accessible.")
        else:
            raise IOError(f"The given log file: {log_file} is a directory. Please provide a valid log file.")
        
    # target does not exist, check perms on parent dir
    parent_dir = os.path.dirname(log_file)
    if not parent_dir: 
        parent_dir = '.'
    # target is creatable if parent dir is writable
    if os.access(parent_dir, os.W_OK):
        return log_file
    else:
        raise IOError(f"The given log file {log_file} is not writable. Please check if it is accessible.")

def check_tsv_sanity(tsv):
    """ 
    Check if the given tsv is readable for the next PPanGGOLiN step.

    :param tsv: Path to the input tsv
    """
    with open(tsv, "r") as f:
        name_set = set()
        duplicated_names = set()
        non_existing_files = set()
        for line in f:
            elements = [el.strip() for el in line.split("\t")]
            if len(elements) <= 1:
                raise Exception(f"No tabulation separator found in given file: {tsv}")
            if " " in elements[0]:
                raise Exception(f"Your genome names contain spaces (The first encountered genome name that had this string:"
                                f" '{elements[0]}'). To ensure compatibility with all of the dependencies of PPanGGOLiN "
                                f"this is not allowed. Please remove spaces from your genome names.")
            old_len = len(name_set)
            name_set.add(elements[0])
            if len(name_set) == old_len:
                duplicated_names.add(elements[0])
            if not os.path.exists(elements[1]):
                non_existing_files.add(elements[1])
        if len(non_existing_files) != 0:
            raise Exception(f"Some of the given files do not exist. The non-existing files are the following : "
                            f"'{' '.join(non_existing_files)}'")
        if len(duplicated_names) != 0:
            raise Exception(f"Some of your genomes have identical names. The duplicated names are the following : "
                            f"'{' '.join(duplicated_names)}'")


def check_input_files(anno: str = None, pangenome: str = None, fasta: str = None):
    """ Checks if the provided input files exist and are of the proper format

    :param anno: Path to the annotation file
    :param pangenome: Path to the pangenome hdf5 file
    :param fasta: path to the fasta file
    """
    if pangenome is not None and not os.path.exists(pangenome):
        raise FileNotFoundError(f"No such file or directory: '{pangenome}'")

    if anno is not None:
        if not os.path.exists(anno):
            raise FileNotFoundError(f"No such file or directory: '{anno}'")
        check_tsv_sanity(anno)

    if fasta is not None:
        if not os.path.exists(fasta):
            raise FileNotFoundError(f"No such file or directory: '{fasta}'")
        check_tsv_sanity(fasta)


def set_verbosity_level(args):
    """Set the verbosity level

    :param args: argument pass by command line
    """
    level = logging.INFO  # info, warnings and errors, default verbose == 1
    if hasattr(args, "verbose"):
        if args.verbose == 2:
            level = logging.DEBUG  # info, debug, warnings and errors
        elif args.verbose == 0:
            level = logging.WARNING  # only warnings and errors

        if args.log != sys.stdout and not args.disable_prog_bar:  # if output is not to stdout we remove progress bars.
            args.disable_prog_bar = True

        logging.basicConfig(stream=args.log, level=level,
                            format='%(asctime)s %(filename)s:l%(lineno)d %(levelname)s\t%(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
        logging.getLogger().info("Command: " + " ".join([arg for arg in sys.argv]))
        logging.getLogger().info("PPanGGOLiN version: " + pkg_resources.get_distribution("ppanggolin").version)


def jaccard_similarities(mat: csc_matrix, jaccard_similarity_th) -> csc_matrix:
    """ Compute the jaccard similarities

    :param mat:
    :param jaccard_similarity_th: threshold

    :return:
    """
    cols_sum = mat.getnnz(axis=0)
    ab = mat.T * mat
    # for rows
    aa = repeat(cols_sum, ab.getnnz(axis=0))
    # for columns
    bb = cols_sum[ab.indices]
    similarities = ab.copy()
    similarities.data /= (aa + bb - ab.data)
    similarities.data[similarities.data < jaccard_similarity_th] = 0
    similarities.eliminate_zeros()
    return similarities


def read_compressed_or_not(file_or_file_path: Union[str, BinaryIO, TextIOWrapper, TextIO]) -> Union[TextIOWrapper,
                                                                                                    BinaryIO, TextIO]:
    """
    Reads a file object or file path, uncompresses it, if need be.

    :param file_or_file_path: Path to the input file

    :return: TextIO object in read only
    """
    file = file_or_file_path
    if isinstance(file, str):
        file = open(file, "rb")
    else:
        try:
            file = open(file.name, "rb")
        except AttributeError:
            return file
    if file.read(2).startswith(b'\x1f\x8b'):
        file.seek(0)
        return TextIOWrapper(gzip.open(filename=file, mode="r"))
    else:
        file.close()
        file = open(file.name, "r")
        return file


def write_compressed_or_not(file_path: str, compress: bool = False) -> Union[gzip.GzipFile, TextIO]:
    """
    Create a file-like object, compressed or not.

    :param file_path: Path to the file
    :param compress: Compress the file in .gz

    :return: file-like object, compressed or not
    """
    if compress:
        return gzip.open(file_path + ".gz", mode="wt")
    else:
        return open(file_path, "w")


def is_compressed(file_or_file_path: Union[str, TextIO, gzip.GzipFile]):
    """ Checks is a file, or file path given is compressed or not

    :param file_or_file_path: Input file

    :return: Get if the file is compressed
    """
    file = file_or_file_path
    if isinstance(file, str):
        file = open(file, "rb")
    else:
        try:
            file = open(file.name, "rb")
        except AttributeError:
            return False
    if file.read(2).startswith(b'\x1f\x8b'):
        return True
    file.close()
    return False


def mk_outdir(output, force):
    """ Create a directory at the given output if it doesn't exist already

    :param output: Path where to create directory
    :param force: Force to write in the directory

    :raise FileExistError: The current path already exist and force is false
    """
    if not os.path.exists(output):
        os.makedirs(output)
    elif not force:
        raise FileExistsError(f"{output} already exists. Use -f if you want to overwrite the files in the directory")


def mk_file_name(basename: str, output: str, force: bool = False) -> Path:
    """Returns a usable filename for a ppanggolin output file, or crashes.

    :param basename: basename for the file
    :param output: Path to save the file
    :param force: Force to write the file

    :return: Path to the file
    """
    filename = Path(output + "/" + basename)
    if filename.suffix != ".h5":
        filename = filename.with_suffix(".h5")

    mk_outdir(output, force)

    if filename.exists() and not force:
        raise FileExistsError(f"{filename.name} already exists. Use -f if you want to overwrite the file")
    return filename


def restricted_float(x) -> float:
    """Decrease the choice possibility of float in argparse

    :param x: given float by user

    :return: given float if it is acceptable

    :raise argparse.ArgumentTypeError: The float is not acceptable
    """
    x = float(x)
    if x < 0.0 or x > 1.0:
        raise argparse.ArgumentTypeError("%r not in range [0.0, 1.0]" % (x,))
    return x


def min_one(x) -> int:
    """Check if the given int is superior to one

    :param x: given float by user

    :return: given float if it is acceptable

    :raise argparse.ArgumentTypeError: The float is not acceptable
    """
    x = int(x)
    if x < 1:
        raise argparse.ArgumentTypeError("%r is inferior to 1" % (x,))
    return x


def connected_components(g: nx.Graph, removed: set, weight: float):
    """
    Yields subgraphs of each connected component you get when filtering edges based on the given weight.

    :param g: Subgraph
    :param removed: removed node
    :param weight: threshold to remove node or not
    """
    for v in g.nodes:
        if v not in removed:
            c = set(_plain_bfs(g, v, removed, weight))
            yield c
            removed.update(c)


def _plain_bfs(g: nx.Graph, source: GeneFamily, removed: set, weight: float):
    """
    A fast BFS node generator, copied from networkx then adapted to the current use case

    :param g: graph with the nodes
    :param source: current node
    :param removed: set of removed nodes
    :param weight:threshold to remove node or not
    """

    nextlevel = {source}
    while nextlevel:
        thislevel = nextlevel
        nextlevel = set()
        for v in thislevel:
            if v not in removed:
                yield v
                removed.add(v)

                for n in g.neighbors(v):
                    if n not in removed:
                        edge_genes_v = g[v][n]["genes"][v]
                        edge_genes_n = g[v][n]["genes"][n]
                        # if the edge is indeed existent for most genes of both families, we use it
                        if len(edge_genes_n) / len(g.nodes[n]["genes"]) >= weight and len(edge_genes_v) / len(
                                g.nodes[v]["genes"]) >= weight:
                            nextlevel.add(n)


def add_gene(obj, gene, fam_split: bool = True):
    """

    :param obj:
    :param gene:
    :param fam_split:
    """
    if fam_split:
        try:
            obj["genes"][gene.family].add(gene)
        except KeyError:
            try:
                obj["genes"][gene.family] = {gene}
            except KeyError:
                obj["genes"] = {gene.family: {gene}}
    else:
        try:
            obj["genes"].add(gene)
        except KeyError:
            obj["genes"] = {gene}


def check_option_workflow(args):
    """
    Check if the given argument to a workflow command is usable

    :param args: list of arguments
    """
    if args.clusters is not None and not any([args.fasta, args.anno]):
        raise Exception("If you give --clusters option, you must give at least --fasta or --anno")

    if not any([args.fasta, args.anno]):
        raise Exception("At least one of --fasta or --anno must be given")


def parse_config_file(yaml_config_file: str) -> dict:
    """
    Parse yaml config file.

    :param yaml_config_file: config file in yaml

    :return: dict of config with key the command and as value another dict with param as key and value as value. 
    """

    with yaml_config_file as yaml_fh:
        config = yaml.safe_load(yaml_fh)
    
    # remove empty section that have no parameter specified in it. In this case they have a None value
    config = {section:param_val_dict for section, param_val_dict in config.items() if param_val_dict is not None}
    return config

def add_common_arguments(subparser: argparse.ArgumentParser):
    """
    Add common argument to the input subparser.

    :param subparser: A subparser object from any subcommand.
    """

    common = subparser._action_groups.pop(1)  # get the 'optional arguments' action group.
    common.title = "Common arguments"
    common.add_argument("--tmpdir", required=False, type=str, default=tempfile.gettempdir(),
                        help="directory for storing temporary files")
    common.add_argument("--verbose", required=False, type=int, default=1, choices=[0, 1, 2],
                        help="Indicate verbose level (0 for warning and errors only, 1 for info, 2 for debug)")
    common.add_argument("--log", required=False, type=check_log, default="stdout", help="log output file")
    common.add_argument("-d", "--disable_prog_bar", required=False, action="store_true",
                        help="disables the progress bars")
    common.add_argument('-f', '--force', action="store_true",
                        help="Force writing in output directory and in pangenome output file.")
        
    common.add_argument("--config", required=False, type=argparse.FileType(), 
                    help="Config file in yaml format to launch the different step of "
                            "the workflow with specific arguments.")

    subparser._action_groups.append(common)


def get_non_default_cli_args(subcomamand_parser: Callable) ->  argparse.Namespace:
    """
    Get args value that have been specified in cmd line.

    This function recreate the same parser than in main but change default value to None 
    in order to distinguish specified and default value

    :param: subparser function used to add subcommand specific arguments 
    """

    parser = argparse.ArgumentParser(prog="", allow_abbrev=True, add_help=False) 
    subparsers = parser.add_subparsers(metavar="", dest="subcommand", title="subcommands", description="")

    sub = subcomamand_parser(subparsers)

    add_common_arguments(sub)

    # set default to None
    for p_action in sub._actions:
        p_action.default = None
        # some args have special type calling a function to trigger something
        # like --log. We need to prevent calling this function a second time.
        if p_action.type in [check_log]:
            p_action.type = None

    cli_args = parser.parse_args()

    # delete args not specified in CLI (so the one with None value) 
    for arg_name, arg_val in cli_args._get_kwargs():
        if arg_val is None:
            delattr(cli_args, arg_name)

    return cli_args

def get_arg_name(arg_val:str):
    """

    """

    if type(arg_val) == TextIOWrapper:
        return arg_val.name
    return arg_val

def overwrite_args(default_args:argparse.Namespace, config_args:argparse.Namespace, cli_args:argparse.Namespace):
    """
    Overwrite args objects.
    When arguments are given in CLI, their value is used instead of the one found in config. 
    When arguments are specified in config they overwrite default values.

    :param default_args: default arguments
    :param config_args: arguments parsed from config file
    :param cli_args: arguments parsed from command line

    :return: final arguments 
    """
    args = argparse.Namespace()
    all_params = [arg for arg in dir(default_args) if not arg.startswith('_')]

    for param in all_params:
        default_val = getattr(default_args, param)
        cli_val = getattr(cli_args, param, 'unspecified')
        config_val = getattr(config_args, param, 'unspecified')

        if param in cli_args:
            # param is defined in cli, cli val is used
            setattr(args, param, cli_val)

            if default_val != cli_val:
                logging.getLogger().debug(f'Parameter "--{param} {get_arg_name(cli_val)}" has been specified in command line.' 
                                        f' Its value overwrites putative config values.')

        elif param in config_args:
            #parma is defined only in config. config val is used
            setattr(args, param, config_val)
            
            if default_val != config_val:
                logging.getLogger().debug(f'Parameter "{param}: {get_arg_name(config_val)}" has been specified in config file with non default value.' 
                                          f' Its value overwrites default value ({get_arg_name(default_val)}).')
        else:
            # param is not defined in cli and in config. default value is applied
            setattr(args, param, default_val)

    return args


def combine_args(args: argparse.Namespace, another_args: argparse.Namespace):
    """
    Combine two args object.

    :param args: initial arguments.
    :param another_args: another args

    :return: object with combined arguments
    """

    other_arg_names = [arg for arg in dir(another_args) if not arg.startswith('_')]

    for arg_name in other_arg_names:
        
        arg_val = getattr(another_args, arg_name)
        setattr(args, arg_name, arg_val)

    return args

def get_args_that_differe_from_default(default_args: argparse.Namespace, final_args: argparse.Namespace, param_to_ignore:list=[]) -> dict:
    """
    Get the parameters that have different value than default values.

    :params default_args: default arguments
    :params final_args: final arguments to compare with default
    :params param_to_ignore: list of params to ignore.

    :return: A dict with param that differ from default as key and the final value of the param as value
    """

    all_params = [arg for arg in dir(final_args) if not arg.startswith('_') if arg not in param_to_ignore]

    params_that_differ = {param:getattr(final_args, param) for param in all_params if getattr(default_args, param) != getattr(final_args, param)}
    
    return params_that_differ


def manage_cli_and_config_args(subcommand: str, config_file:str, subcommand_to_subparser:dict) -> argparse.Namespace:
    """
    Manage command line and config arguments for the given subcommand.

    This function parse arguments from the cmd line and config file and set up the following priority: cli > config > default
    When the subcommand is a workflow, the subcommand used in worflows are also parsed in the config.  


    :params subcommand: Name of the subcommand.
    :params config_file: Path to the config file given in argument. If None, only default and cli arguments value are used.
    :params subcommand_to_subparser: Dict with subcommand name as key and the corresponding subparser function as value. 
    """


    if config_file:
        config = parse_config_file(config_file)
    else:
        config = {}

    # convert config dict to defaultdict 
    config = defaultdict(dict, config)

    cmd_subparser = subcommand_to_subparser[subcommand]

    
    default_args = get_default_args(subcommand, cmd_subparser)
    
    cli_args = get_cli_args(cmd_subparser)
    
    all_cmd_param_names = {arg_name for arg_name in dir(default_args) if not arg_name.startswith('_')}

    input_params = {param for param in all_cmd_param_names if param in ALL_INPUT_PARAMS}

    general_params = {param for param in all_cmd_param_names if param in ALL_GENERAL_PARAMS}
    
    specific_params  = all_cmd_param_names - (input_params | general_params)

    all_unspecific_params = ALL_INPUT_PARAMS + ALL_GENERAL_PARAMS
    # manage logging first to correctly set it up and to be able to log any issue when using config file later on
    config_general_args = get_config_args(subcommand, cmd_subparser, config, "general_parameters", general_params, strict_config_check=False)
    general_args = overwrite_args(default_args, config_general_args, cli_args)

    set_verbosity_level(general_args)

    
    config_input_args = get_config_args(subcommand, cmd_subparser, config, "input_parameters", input_params, strict_config_check=True)



    if subcommand in WORKFLOW_SUBCOMMANDS:
        # for workflow commands there is no section dedicated in the config: so no specific_args 
        # only general_parameters and  sections of commands launched in the worklow commands are used
        config_args = combine_args(config_general_args, config_input_args)
    else:
        config_specific_args = get_config_args(subcommand, cmd_subparser, config, subcommand, specific_params, strict_config_check=True)
        config_args = combine_args(config_general_args, config_specific_args)
        config_args = combine_args(config_args, config_input_args)

    # manage priority between source of args 
    # cli > config > default

    args = overwrite_args(default_args, config_args, cli_args)
    params_that_differ = get_args_that_differe_from_default(default_args, args, input_params)
    
    if params_that_differ:
        params_that_differ_str = ', '.join([f'{p}={v}' for p,v in params_that_differ.items()])
        logging.getLogger().debug(f"{len(params_that_differ)} {subcommand} parameters have non-default value: {params_that_differ_str}")

    # manage workflow command
    if subcommand in WORKFLOW_SUBCOMMANDS:
        for workflow_step in ALL_WORKFLOW_DEPENDENCIES:
            if workflow_step in ["rgp", "spot"] and subcommand in ["workflow", "panmodule"]:
                continue
            elif  workflow_step == "module" and subcommand in ["workflow", "panmodule"]:
                continue

            logging.getLogger().debug(f'Parsing {workflow_step} arguments in config file.')
            step_subparser = subcommand_to_subparser[workflow_step]

            default_step_args = get_default_args(workflow_step, step_subparser, unwanted_args=all_unspecific_params)

            # remove general args
            all_param_names = {arg_name for arg_name in dir(default_step_args) if not arg_name.startswith('_')}
            specific_step_params = {param_name for param_name in all_param_names if param_name not in all_unspecific_params}
            config_step_args = get_config_args(workflow_step, step_subparser, config, workflow_step, specific_step_params, strict_config_check=True)

            # overwrite write default when not specified in config 
            # if workflow_step == 'write':
            #     for out_flag in WRITE_FLAG_DEFAULT_IN_WF:
            #         setattr(default_step_args, out_flag, True)
            
            step_args = overwrite_args(default_step_args, config_step_args, cli_args)
            
            step_params_that_differ = get_args_that_differe_from_default(default_step_args, step_args)

            if step_params_that_differ:
                step_params_that_differ_str = ', '.join([f'{p}={v}' for p,v in step_params_that_differ.items()])
                logging.getLogger().debug(f"{len(step_params_that_differ)} {workflow_step} parameters have a non-default value: {step_params_that_differ_str}")

            # add step name to differentiate the params
            step_params_that_differ = {f'{workflow_step}:{param}':value for param, value in step_params_that_differ.items()}

            params_that_differ.update(step_params_that_differ)

            
            # Add args namespace of the step to the inital args namespace
            setattr(args, workflow_step, step_args)
    
    if params_that_differ:
        logging.getLogger().info(f'{len(params_that_differ)} parameters have a non-default value.')

    check_config_consistency(config, ALL_WORKFLOW_DEPENDENCIES)
    
    return args

def check_config_consistency(config: dict, workflow_steps:list):
    """
    Check that the same parameter used in different subcommand inside a workflow has the same value. 

    If not, the function throw a logging.warning. 

    :params config_dict: config dict with as key the section of the config file and as value another dict pairing name and value of parameters.
    :params workflow_steps: list of subcommand names used in the workflow execution.
    """
    def count_different_values(values):
        hashable_values = set()
        for value in values:
            hashable_value = tuple(value) if type(value) == list else value
            hashable_values.add(hashable_value)
        return len(hashable_values)
    
    # params used in multiple subcommands
    all_params = [param for subcmd, param_to_value_dict in config.items() for param in param_to_value_dict if subcmd in workflow_steps]
    duplicate_params = [param for param in all_params if all_params.count(param) > 1]

    for duplicate_param in set(duplicate_params):
        step_to_value = {step:param_to_value[duplicate_param] for step, param_to_value in config.items() if duplicate_param in param_to_value}

        if count_different_values(step_to_value.values()) > 1:
            logging.warning(f'The parameter {duplicate_param} used in multiple subcommands of the workflow is specified with different values in config file: {step_to_value}.')


def set_up_config_param_to_parser(config_param_val: dict) -> list:
    """
    Take dict pairing parameters and values and format the corresponding list of arguments to feed a parser.

    When the parameter value is False, the parameter is a flag and thus is not added to the list.

    :params config_param_val: Dict with parameter name as key and parameter value as value.

    :return: list of argument strings formated for an argparse.ArgumentParser object.
    """

    arguments_to_parse = []
    for param, val in config_param_val.items():

        if type(val) == bool:
            # param is a flag
            if val is True:
                arguments_to_parse.append(f"--{param}")
        else:
            arguments_to_parse.append(f"--{param}")

            if type(val) == list:
                # range of values need to be added one by one
                arguments_to_parse += [str(v) for v in val]
            else:
                arguments_to_parse.append(str(val))
    return arguments_to_parse

def get_subcommand_parser(subparser_fct: Callable, name:str='') -> tuple([argparse._SubParsersAction, argparse.ArgumentParser]):
    """
    Get subcommand parser object using the given subparser function.

    Common arguments are also added to the parser object.

    :params subparser_fct:
    :name: Name of section to add more info in the parser in case of error.

    :return: The parser and subparser objects
    """
    prog = ""
    usage = ""

    if name:
        prog = f"Parsing section {name} in config file"
        usage = "Yaml config file"

    parser = argparse.ArgumentParser(prog=prog, 
                                    allow_abbrev=False, add_help=False) 
    
    subparsers = parser.add_subparsers(metavar="", dest="subcommand", title="subcommands", description="")

    sub = subparser_fct(subparsers)
    sub.usage  = usage
    add_common_arguments(sub)

    # set off required flag in required arguments
    for arg_action in sub._actions:
        if arg_action.required:
            arg_action.required = False
    return parser, sub

def get_default_args(subcommand:str, subparser_fct: Callable, unwanted_args:list =[]) -> argparse.Namespace:
    """
    Get default value for the arguments for the given subparser function.

    :params subcommand: Name of the ppanggolin subcommand.
    :params subparser_fct: Subparser function to use. This subparser give the expected argument for the subcommand.
    :params unwanted_args: List of arguments to filter out.

    :return args: arguments with default values. 
    """

    parser, sub = get_subcommand_parser(subparser_fct, subcommand)
    
    # remove unwanted argumnents
    sub._actions = [p_action for p_action in sub._actions if p_action.dest not in unwanted_args]

    args = parser.parse_args([subcommand])

    return args

def get_config_args(subcommand: str, subparser_fct: Callable, config_dict: dict, config_section:str, 
                    expected_params:list, strict_config_check:bool) -> argparse.Namespace:
    """
    Parsing parameters of a specific section of the config file.

    If some parameter are not specified in the config they are not added to the args object.

    :params subcommand: Name of the ppanggolin subcommand.
    :params subparser_fct: Subparser function to use. This subparser give the expected argument for the subcommand.
    :params config_dict: config dict with as key the section of the config file and as value another dict pairing name and value of parameters.
    :params config_section: Which section to parse in config file.
    :params expected_params: List of argument to expect in the parser. If the parser has other arguments, these arguments are filtered out.
    :params strict_config_check: if set to true, an error is raised when a parameter is found in the config and it is not in the expected_params list.

    :return args: Arguments parse from the config
    """
    config = config_dict[config_section]

    parser, sub = get_subcommand_parser(subparser_fct, subcommand)

    # for all args set default to None to be able to distinguish params that have been specified in config
    erase_default_value(sub)

    # Manage args
    sub._actions = [p_action for p_action in sub._actions if p_action.dest in expected_params]
            
    if not strict_config_check:
        # remove param found in config that are not expected by parser. useful for general_parameters.
        expected_args_names = [p_action.dest for p_action in sub._actions]
        unexpected_config = [f'{name}:{value}' for name,value in config.items() if name not in expected_args_names]
        config = {name:value for name,value in config.items() if name in expected_args_names}

        if unexpected_config:
            logging.info(f'While parsing {config_section} section in config file, {len(unexpected_config)} unexpected parameters '
                            f'were ignored : {" ".join(unexpected_config)}')
    else:
        for param_name in config:
            if param_name not in expected_params:
                sub.error(f"unrecognized arguments: {param_name}")


    config_args_to_parse = set_up_config_param_to_parser(config)

    args = parser.parse_args([subcommand] + config_args_to_parse)

    # remove argument that have not been specified in config file
    # unspecified argument have None as value
    delete_unspecified_args(args)

    return args

def get_cli_args(subparser_fct: Callable) -> argparse.Namespace:
    """
    Parse command line arguments using the specified parsing function. 

    :params subparser_fct: Subparser function to use. This subparser give the expected argument for the subcommand.
    """
    
    parser, sub = get_subcommand_parser(subparser_fct)

    # for all args set default to None to be able to distinguish params that have been specified in config
    erase_default_value(sub)

    cli_args = parser.parse_args() # parse cli
    
    # remove argument that have not been specified
    delete_unspecified_args(cli_args)
    delattr(cli_args, 'subcommand')
    if 'config' in cli_args:
        delattr(cli_args, 'config')

    return cli_args


def erase_default_value(parser : argparse.ArgumentParser):
    """
    Remove default action in the given list of argument parser actions. 

    This is dnoe to distinguish specified arguments.

    :params parser: An argparse.ArgumentParser object with default values to erase.
    """

    # for all args set default to None
    for p_action in parser._actions:
        p_action.default = None

def delete_unspecified_args(args:  argparse.Namespace):
    """
    Delete argument from the given argparse.Namespace with None values.

    :param args: arguments to filter.
    """

    for arg_name, arg_val in args._get_kwargs():
        if arg_val is None:
            delattr(args, arg_name)

