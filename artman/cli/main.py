# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The new artman CLI with the following syntax.

    artman [Options] generate <artifact_name>

.. note::
    Only local execution is supported at this moment. The CLI syntax is
    beta, and might have changes in the future.
"""

from __future__ import absolute_import
from logging import INFO
import argparse
from distutils.dir_util import copy_tree
import io
import os
import pprint
import subprocess
import sys
import tempfile
import traceback
import warnings

import pkg_resources
from ruamel import yaml
from taskflow import engines

from artman.config import converter, loader
from artman.config.proto.config_pb2 import Artifact, Config
from artman.config.proto.user_config_pb2 import UserConfig
from artman.cli import support
from artman.pipelines import pipeline_factory
from artman.utils import config_util
from artman.utils.logger import logger, setup_logging

VERSION = pkg_resources.get_distribution('googleapis-artman').version
ARTMAN_DOCKER_IMAGE = 'googleapis/artman:%s' % VERSION
RUNNING_IN_ARTMAN_DOCKER_TOKEN = 'RUNNING_IN_ARTMAN_DOCKER'
DEFAULT_OUTPUT_DIR = './artman-genfiles'

def main(*args):
    """Main method of artman."""
    # If no arguments are sent, we are using the entry point; derive
    # them from sys.argv.
    if not args:
        args = sys.argv[1:]

    # Get to a normalized set of arguments.
    flags = parse_args(*args)
    user_config = loader.read_user_config(flags.user_config)
    _adjust_root_dir(flags.root_dir)
    pipeline_name, pipeline_kwargs = normalize_flags(flags, user_config)

    if flags.local:
        try:
            pipeline = pipeline_factory.make_pipeline(pipeline_name,
                                                      **pipeline_kwargs)
            # Hardcoded to run pipeline in serial engine, though not necessarily.
            engine = engines.load(
                pipeline.flow, engine='serial', store=pipeline.kwargs)
            engine.run()
        except:
            logger.error(traceback.format_exc())
            sys.exit(32)
        finally:
            _change_owner(flags, pipeline_name, pipeline_kwargs)
    else:
        support.check_docker_requirements(flags.image)
        # Note: artman currently won't work if input directory doesn't contain
        # common-protos.
        logger.info('Running artman command in a Docker instance.')
        _run_artman_in_docker(flags)


def _adjust_root_dir(root_dir):
    """Adjust input directory to use versioned common config and/or protos.

    Some common protos will be needed during protoc compilation, but are not
    provided by users in some cases. When such shared proto directories are
    not provided, we copy and use the versioned ones.
    """
    if os.getenv(RUNNING_IN_ARTMAN_DOCKER_TOKEN):
        # Only doing this when running inside Docker container
        common_proto_dirs = [
            'google/api',
            'google/iam/v1',
            'google/longrunning',
            'google/rpc',
            'google/type',
        ]
        # /googleapis is the root of the versioned googleapis repo
        # inside Artman Docker image.
        for src_dir in common_proto_dirs:
            if not os.path.exists(os.path.join(root_dir, src_dir)):
                copy_tree(os.path.join('/googleapis', src_dir),
                          os.path.join(root_dir, src_dir))


def parse_args(*args):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s {0}'.format(VERSION),
    )
    parser.add_argument(
        '--config',
        type=str,
        default='artman.yaml',
        help='[Optional] Specify path to artman config yaml, which can be '
        'either an absolute path, or a path relative to the input '
        'directory (specified by `--root-dir` flag). Default to '
        '`artman.yaml`', )
    parser.add_argument(
        '--output-dir',
        type=str,
        default=DEFAULT_OUTPUT_DIR,
        help='[Optional] Directory to store output generated by artman. '
        'Default to `' + DEFAULT_OUTPUT_DIR + '`', )
    parser.add_argument(
        '--root-dir',
        type=str,
        default='',
        help='[Optional] Directory with all input that is needed by artman, '
        'which includes but is not limited to API protos, service config yaml '
        'and GAPIC config yaml. It will be passed to protobuf compiler via '
        '`-I` flag in order to generate descriptor. If not specified, it '
        'will use the current working directory.',
    )
    parser.add_argument(
        '-v',
        '--verbose',
        action='store_const',
        const=10,
        default=None,
        dest='verbosity',
        help='Show verbose / debug output.', )
    parser.add_argument(
        '--user-config',
        default='~/.artman/config.yaml',
        help='[Optional] User configuration file to stores credentials like '
        'GitHub credentials. Default to `~/.artman/config.yaml`', )
    parser.add_argument(
        '--local',
        dest='local',
        action='store_true',
        help='[Optional] If specified, running the artman on the local host '
        'machine instead of artman docker instance that have all binaries '
        'installed. Note: one will have to make sure all binaries get '
        'installed on the local machine with this flag, a full list can '
        'be found at '
        'https://github.com/googleapis/artman/blob/master/Dockerfile', )
    parser.set_defaults(local=False)
    parser.add_argument(
        '--image',
        default=ARTMAN_DOCKER_IMAGE,
        help=('[Optional] Specify docker image used by artman when running in '
              'a Docker instance. Default to `%s`' % ARTMAN_DOCKER_IMAGE))
    parser.add_argument(
        '--generator-args',
        type=str,
        default=None,
        help='Additional arguments to pass to gapic-generator')


    # Add sub-commands.
    subparsers = parser.add_subparsers(
        dest='subcommand', help='Support [generate] sub-commands')

    # `generate` sub-command.
    parser_generate = subparsers.add_parser(
        'generate', help='Generate artifact')
    parser_generate.add_argument(
        'artifact_name',
        type=str,
        help='[Required] Name of the artifact for artman to generate. Must '
        'match an artifact in the artman config yaml.')
    parser_generate.add_argument(
        '--aspect',
        type=str,
        default=None,
        help='[Optional] Aspect of output to generate: ALL, CODE, or PACKAGE')

    return parser.parse_args(args=args)


def normalize_flags(flags, user_config):
    """Combine the argparse flags and user configuration together.

    Args:
        flags (argparse.Namespace): The flags parsed from sys.argv
        user_config (dict): The user configuration taken from
                            ~/.artman/config.yaml.

    Returns:
        tuple (str, dict): 2-tuple containing:
            - pipeline name
            - pipeline arguments
    """
    if flags.root_dir:
        flags.root_dir = os.path.abspath(flags.root_dir)
        flags.config = os.path.join(flags.root_dir, flags.config)
    else:
        flags.root_dir = os.getcwd()
        flags.config = os.path.abspath(flags.config)
    root_dir = flags.root_dir
    flags.output_dir = os.path.abspath(flags.output_dir)
    pipeline_args = {}

    # Determine logging verbosity and then set up logging.
    verbosity = INFO
    if getattr(flags, 'verbosity', None):
        verbosity = getattr(flags, 'verbosity')
    setup_logging(verbosity)

    # Save local paths, if applicable.
    # This allows the user to override the path to api-client-staging or
    # toolkit on his or her machine.
    pipeline_args['root_dir'] = root_dir
    pipeline_args['toolkit_path'] = user_config.local.toolkit
    pipeline_args['generator_args'] = flags.generator_args

    artman_config_path = flags.config
    if not os.path.isfile(artman_config_path):
        logger.error(
            'Artman config file `%s` doesn\'t exist.' % artman_config_path)
        sys.exit(96)

    try:
        artifact_config = loader.load_artifact_config(
            artman_config_path, flags.artifact_name, flags.aspect)
    except ValueError as ve:
        logger.error('Artifact config loading failed with `%s`' % ve)
        sys.exit(96)

    legacy_config_dict = converter.convert_to_legacy_config_dict(
        artifact_config, root_dir, flags.output_dir)
    logger.debug('Below is the legacy config after conversion:\n%s' %
                 pprint.pformat(legacy_config_dict))

    language = Artifact.Language.Name(
        artifact_config.language).lower()

    # 2020-05-26 @alexander-fenster
    # We only use Artman for generating Ruby libraries, others use Bazel.
    # Print a proper deprecation message.
    if language != Artifact.RUBY:
        warnings.warn("*** WARNING: *** Artman is deprecated for all languages other than Ruby", DeprecationWarning)

    # Set the pipeline
    artifact_type = artifact_config.type
    pipeline_args['artifact_type'] = Artifact.Type.Name(artifact_type)
    pipeline_args['aspect'] = Artifact.Aspect.Name(artifact_config.aspect)
    if artifact_type == Artifact.GAPIC_ONLY:
        pipeline_name = 'GapicOnlyClientPipeline'
        pipeline_args['language'] = language
    elif artifact_type == Artifact.GAPIC:
        pipeline_name = 'GapicClientPipeline'
        pipeline_args['language'] = language
    elif artifact_type == Artifact.DISCOGAPIC:
        pipeline_name = 'DiscoGapicClientPipeline'
        pipeline_args['language'] = language
        pipeline_args['discovery_doc'] = artifact_config.discovery_doc
    elif artifact_type == Artifact.GRPC:
        pipeline_name = 'GrpcClientPipeline'
        pipeline_args['language'] = language
    elif artifact_type == Artifact.GAPIC_CONFIG:
        pipeline_name = 'GapicConfigPipeline'
    elif artifact_type == Artifact.DISCOGAPIC_CONFIG:
        pipeline_name = 'DiscoGapicConfigPipeline'
        pipeline_args['discovery_doc'] = artifact_config.discovery_doc
        if os.path.abspath(flags.output_dir) != os.path.abspath(DEFAULT_OUTPUT_DIR):
            logger.warning("`output_dir` is ignored in DiscoGapicConfigGen. "
             + "Yamls are saved at the path specified by `gapic_yaml`.")
        pipeline_args['output_dir'] = tempfile.mkdtemp()
    elif artifact_type == Artifact.PROTOBUF:
        pipeline_name = 'ProtoClientPipeline'
        pipeline_args['language'] = language
    else:
        raise ValueError('Unrecognized artifact.')

    # Parse out the full configuration.
    config_args = config_util.load_config_spec(legacy_config_dict, language)
    config_args.update(pipeline_args)
    pipeline_args = config_args
    # Print out the final arguments to stdout, to help the user with
    # possible debugging.
    pipeline_args_repr = yaml.dump(
        pipeline_args,
        block_seq_indent=2,
        default_flow_style=False,
        indent=2, )
    logger.info('Final args:')
    for line in pipeline_args_repr.split('\n'):
        if 'token' in line:
            index = line.index(':')
            line = line[:index + 2] + '<< REDACTED >>'
        logger.info('  {0}'.format(line))

    # Return the final arguments.
    return pipeline_name, pipeline_args

def _run_artman_in_docker(flags):
    """Executes artman command.

    Args:
        root_dir: The input directory that will be mounted to artman docker
            container as local googleapis directory.
    Returns:
        The output directory with artman-generated files.
    """
    ARTMAN_CONTAINER_NAME = 'artman-docker'
    root_dir = flags.root_dir
    output_dir = flags.output_dir
    artman_config_dirname = os.path.dirname(flags.config)
    docker_image = flags.image

    inner_artman_cmd_str = ' '.join(["'" + arg + "'" for arg in sys.argv[1:]])
    # Because artman now supports setting root dir in either command line or
    # user config, make sure `--root-dir` flag gets explicitly passed to the
    # artman command running inside Artman Docker container.
    if '--root-dir' not in inner_artman_cmd_str:
      inner_artman_cmd_str = '--root-dir %s %s' % (
          root_dir, inner_artman_cmd_str)

    # TODO(ethanbao): Such folder to folder mounting won't work on windows.
    base_cmd = [
        'docker', 'run', '--name', ARTMAN_CONTAINER_NAME, '--rm', '-i', '-t',
        '-e', 'HOST_USER_ID=%s' % os.getuid(),
        '-e', 'HOST_GROUP_ID=%s' % os.getgid(),
        '-e', '%s=True' % RUNNING_IN_ARTMAN_DOCKER_TOKEN,
        '-v', '%s:%s' % (root_dir, root_dir),
        '-v', '%s:%s' % (output_dir, output_dir),
        '-v', '%s:%s' % (artman_config_dirname, artman_config_dirname),
        '-w', root_dir
    ]
    base_cmd.extend([docker_image, '/bin/bash', '-c'])

    inner_artman_debug_cmd_str = inner_artman_cmd_str
    # Because debug_cmd is run inside the Docker image, we want to
    # make sure --local is set
    if '--local' not in inner_artman_debug_cmd_str:
        inner_artman_debug_cmd_str = '--local %s' % inner_artman_debug_cmd_str
    debug_cmd = list(base_cmd)
    debug_cmd.append('"artman %s; bash"' % inner_artman_debug_cmd_str)

    cmd = base_cmd
    cmd.append('artman --local %s' % (inner_artman_cmd_str))
    try:
        output = subprocess.check_output(cmd)
        logger.info(output.decode('utf8'))
        return output_dir
    except subprocess.CalledProcessError as e:
        logger.error(e.output.decode('utf8'))
        logger.error(
            'Artman execution failed. For additional logging, re-run the '
            'command with the "--verbose" flag')
        sys.exit(32)
    finally:
        logger.debug('For further inspection inside docker container, run `%s`'
                     % ' '.join(debug_cmd))


def _change_owner(flags, pipeline_name, pipeline_kwargs):
    """Change file/directory ownership if necessary."""
    user_host_id = int(os.getenv('HOST_USER_ID', 0))
    group_host_id = int(os.getenv('HOST_GROUP_ID', 0))
    # When artman runs in Docker instance, all output files are by default
    # owned by `root`, making it non-editable by Docker host user. When host
    # user id and group id get passed through environment variables via
    # Docker `-e` flag, artman will change the owner based on the specified
    # user id and group id.
    if not user_host_id or not group_host_id:
        return
    # Change ownership of output directory.
    if os.path.exists(flags.output_dir):
        _change_directory_owner(flags.output_dir, user_host_id, group_host_id)

    # Change the local repo directory if specified.
    if 'local_repo_dir' in pipeline_kwargs:
        local_repo_dir = pipeline_kwargs['local_repo_dir']
        if (os.path.exists(local_repo_dir)):
            _change_directory_owner(local_repo_dir, user_host_id, group_host_id)

    if pipeline_kwargs['gapic_yaml']:
        gapic_config_path = pipeline_kwargs['gapic_yaml']
        if (os.path.exists(gapic_config_path) and
            ('GapicConfigPipeline' == pipeline_name
                or 'DiscoGapicConfigPipeline' == pipeline_name)):
            # There is a trick that the gapic config output is generated to
            # input directory, where it is supposed to be in order to be
            # used as an input for other artifact generation. With that
            # the gapic config output is not located in the output directory,
            # but the input directory. Make the explicit chown in this case.
            os.chown(gapic_config_path, user_host_id, group_host_id)


def _change_directory_owner(directory, user_host_id, group_host_id):
    """Change ownership recursively for everything under the given directory."""
    for root, dirs, files in os.walk(directory):
        os.chown(root, user_host_id, group_host_id)
        for d in dirs:
            os.chown(
                os.path.join(root, d), user_host_id, group_host_id)
        for f in files:
            os.chown(
                os.path.join(root, f), user_host_id, group_host_id)

if __name__ == "__main__":
    main()
