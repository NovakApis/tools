#!/usr/bin/env python
""" Launch a pipeline, interactively collecting params """

from __future__ import print_function

import click
import copy
import json
import logging
import os
import PyInquirer
import re
import subprocess
import textwrap
import webbrowser

import nf_core.schema, nf_core.utils

#
# NOTE: WE ARE USING A PRE-RELEASE VERSION OF PYINQUIRER
#
# This is so that we can capture keyboard interruptions in a nicer way
# with the raise_keyboard_interrupt=True argument in the PyInquirer.prompt() calls
# It also allows list selections to have a default set.
#
# Waiting for a release of version of >1.0.3 of PyInquirer.
# See https://github.com/CITGuru/PyInquirer/issues/90
#
# When available, update setup.py to use regular pip version

class Launch(object):
    """ Class to hold config option to launch a pipeline """

    def __init__(self, pipeline, revision=None, command_only=False, params_in=None, params_out=None, save_all=False, show_hidden=False, url=None, web_id=None):
        """Initialise the Launcher class

        Args:
          schema: An nf_core.schema.PipelineSchema() object
        """

        self.pipeline = pipeline
        self.pipeline_revision = revision
        self.schema_obj = None
        self.use_params_file = False if command_only else True
        self.params_in = params_in
        self.params_out = params_out if params_out else os.path.join(os.getcwd(), 'nf-params.json')
        self.save_all = save_all
        self.show_hidden = show_hidden
        self.web_schema_launch_url = url if url else 'https://nf-co.re/json_schema_launch'
        self.web_schema_launch_web_url = None
        self.web_schema_launch_api_url = None
        if web_id:
            self.web_schema_launch_web_url = '{}?id={}'.format(self.web_schema_launch_url, web_id)
            self.web_schema_launch_api_url = '{}?id={}&api=true'.format(self.web_schema_launch_url, web_id)

        self.nextflow_cmd = 'nextflow run {}'.format(self.pipeline)

        # Prepend property names with a single hyphen in case we have parameters with the same ID
        self.nxf_flag_schema = {
            'Nextflow command-line flags': {
                'type': 'object',
                'description': 'General Nextflow flags to control how the pipeline runs.',
                'help_text': "These are not specific to the pipeline and will not be saved in any parameter file. They are just used when building the `nextflow run` launch command.",
                'properties': {
                    '-name': {
                        'type': 'string',
                        'description': 'Unique name for this nextflow run',
                        'pattern': '^[a-zA-Z0-9-_]+$'
                    },
                    '-revision': {
                        'type': 'string',
                        'description': 'Pipeline release / branch to use',
                        'help_text': 'Revision of the project to run (either a git branch, tag or commit SHA number)'
                    },
                    '-profile': {
                        'type': 'string',
                        'description': 'Configuration profile'
                    },
                    '-work-dir': {
                        'type': 'string',
                        'description': 'Work directory for intermediate files',
                        'default': os.getenv('NXF_WORK') if os.getenv('NXF_WORK') else './work',
                    },
                    '-resume': {
                        'type': 'boolean',
                        'description': 'Resume previous run, if found',
                        'help_text': """
                            Execute the script using the cached results, useful to continue
                            executions that was stopped by an error
                        """,
                        'default': False
                    }
                }
            }
        }
        self.nxf_flags = {}
        self.params_user = {}

    def launch_pipeline(self):

        # Check if the output file exists already
        if os.path.exists(self.params_out):
            logging.warning("Parameter output file already exists! {}".format(os.path.relpath(self.params_out)))
            if click.confirm(click.style('Do you want to overwrite this file? ', fg='yellow')+click.style('[y/N]', fg='red'), default=False, show_default=False):
                os.remove(self.params_out)
                logging.info("Deleted {}\n".format(self.params_out))
            else:
                logging.info("Exiting. Use --params-out to specify a custom filename.")
                return False


        logging.info("This tool ignores any pipeline parameter defaults overwritten by Nextflow config files or profiles\n")

        # Build the schema and starting inputs
        if self.get_pipeline_schema() is False:
            return False
        self.set_schema_inputs()
        self.merge_nxf_flag_schema()

        if self.prompt_web_gui():
            try:
                self.launch_web_gui()
            except AssertionError as e:
                logging.error(click.style(e.args[0], fg='red'))
                return False
        else:
            # Kick off the interactive wizard to collect user inputs
            self.prompt_schema()

        # Validate the parameters that we now have
        if not self.schema_obj.validate_params():
            return False

        # Strip out the defaults
        if not self.save_all:
            self.strip_default_params()

        # Build and launch the `nextflow run` command
        self.build_command()
        self.launch_workflow()

    def get_pipeline_schema(self):
        """ Load and validate the schema from the supplied pipeline """

        # Get the schema
        self.schema_obj = nf_core.schema.PipelineSchema()
        try:
            # Get schema from name, load it and lint it
            self.schema_obj.get_schema_path(self.pipeline, revision=self.pipeline_revision)
            self.schema_obj.load_lint_schema()
        except AssertionError:
            # No schema found
            # Check that this was actually a pipeline
            if self.schema_obj.pipeline_dir is None or not os.path.exists(self.schema_obj.pipeline_dir):
                logging.error("Could not find pipeline: {}".format(self.pipeline))
                return False
            if not os.path.exists(os.path.join(self.schema_obj.pipeline_dir, 'nextflow.config')) and not os.path.exists(os.path.join(self.schema_obj.pipeline_dir, 'main.nf')):
                logging.error("Could not find a main.nf or nextfow.config file, are you sure this is a pipeline?")
                return False

            # Build a schema for this pipeline
            logging.info("No pipeline schema found - creating one from the config")
            try:
                self.schema_obj.get_wf_params()
                self.schema_obj.make_skeleton_schema()
                self.schema_obj.remove_schema_notfound_configs()
                self.schema_obj.add_schema_found_configs()
                self.schema_obj.flatten_schema()
                self.schema_obj.get_schema_defaults()
            except AssertionError as e:
                logging.error("Could not build pipeline schema: {}".format(e))
                return False

    def set_schema_inputs(self):
        """
        Take the loaded schema and set the defaults as the input parameters
        If a nf_params.json file is supplied, apply these over the top
        """
        # Set the inputs to the schema defaults
        self.schema_obj.input_params = copy.deepcopy(self.schema_obj.schema_defaults)

        # If we have a params_file, load and validate it against the schema
        if self.params_in:
            logging.info("Loading {}".format(self.params_in))
            self.schema_obj.load_input_params(self.params_in)
            self.schema_obj.validate_params()

    def merge_nxf_flag_schema(self):
        """ Take the Nextflow flag schema and merge it with the pipeline schema """
        # Do it like this so that the Nextflow params come first
        schema_params = self.nxf_flag_schema
        schema_params.update(self.schema_obj.schema['properties'])
        self.schema_obj.schema['properties'] = schema_params

    def prompt_web_gui(self):
        """ Ask whether to use the web-based or cli wizard to collect params """

        # Check whether --id was given and we're loading params from the web
        if self.web_schema_launch_web_url is not None and self.web_schema_launch_api_url is not None:
            return True

        click.secho("\nWould you like to enter pipeline parameters using a web-based interface or a command-line wizard?\n", fg='magenta')
        question = {
            'type': 'list',
            'name': 'use_web_gui',
            'message': 'Choose launch method',
            'choices': [
                'Web based',
                'Command line'
            ]
        }
        answer = PyInquirer.prompt([question], raise_keyboard_interrupt=True)
        return answer['use_web_gui'] == 'Web based'

    def launch_web_gui(self):
        """ Send schema to nf-core website and launch input GUI """

        # If --id given on the command line, we already know the URLs
        if self.web_schema_launch_web_url is None and self.web_schema_launch_api_url is None:
            content = {
                'post_content': 'json_schema_launcher',
                'api': 'true',
                'version': nf_core.__version__,
                'status': 'waiting_for_user',
                'schema': json.dumps(self.schema_obj.schema),
                'nxf_flags': json.dumps(self.nxf_flags),
                'input_params': json.dumps(self.schema_obj.input_params)
            }
            web_response = nf_core.utils.poll_nfcore_web_api(self.web_schema_launch_url, content)
            try:
                assert 'api_url' in web_response
                assert 'web_url' in web_response
                assert web_response['status'] == 'recieved'
            except (AssertionError) as e:
                logging.debug("Response content:\n{}".format(json.dumps(web_response, indent=4)))
                raise AssertionError("JSON Schema builder response not recognised: {}\n See verbose log for full response (nf-core -v launch)".format(self.web_schema_launch_url))
            else:
                self.web_schema_launch_web_url = web_response['web_url']
                self.web_schema_launch_api_url = web_response['api_url']

        # ID supplied - has it been completed or not?
        else:
            logging.debug("ID supplied - checking status at {}".format(self.web_schema_launch_api_url))
            if self.get_web_launch_response():
                return True

        # Launch the web GUI
        logging.info("Opening URL: {}".format(self.web_schema_launch_web_url))
        webbrowser.open(self.web_schema_launch_web_url)
        logging.info("Waiting for form to be completed in the browser. Remember to click Finished when you're done.\n")
        nf_core.utils.wait_cli_function(self.get_web_launch_response)

    def get_web_launch_response(self):
        """
        Given a URL for a web-gui launch response, recursively query it until results are ready.
        """
        web_response = nf_core.utils.poll_nfcore_web_api(self.web_schema_launch_api_url)
        if web_response['status'] == 'error':
            raise AssertionError("Got error from launch API ({})".format(web_response.get('message')))
        elif web_response['status'] == 'waiting_for_user':
            return False
        elif web_response['status'] == 'launch_params_complete':
            logging.info("Found completed parameters from nf-core launch GUI")
            try:
                self.nxf_flags = web_response['nxf_flags']
                self.schema_obj.input_params = web_response['input_params']
                self.sanitise_web_response()
            except json.decoder.JSONDecodeError as e:
                raise AssertionError("Could not load JSON response from web API: {}".format(e))
            except KeyError as e:
                raise AssertionError("Missing return key from web API: {}".format(e))
            except Exception as e:
                logging.debug(web_response)
                raise AssertionError("Unknown exception - see verbose log for details: {}".format(e))
            return True
        else:
            logging.debug("Response content:\n{}".format(json.dumps(web_response, indent=4)))
            raise AssertionError("Web launch GUI returned unexpected status ({}): {}\n See verbose log for full response".format(web_response['status'], self.web_schema_launch_api_url))

    def sanitise_web_response(self):
        """
        The web builder returns everything as strings.
        Use the functions defined in the cli wizard to convert to the correct types.
        """
        # Collect pyinquirer objects for each defined input_param
        pyinquirer_objects = {}
        for param_id, param_obj in self.schema_obj.schema['properties'].items():
            if(param_obj['type'] == 'object'):
                for child_param_id, child_param_obj in param_obj['properties'].items():
                    pyinquirer_objects[child_param_id] = self.single_param_to_pyinquirer(child_param_id, child_param_obj, print_help=False)
            else:
                pyinquirer_objects[param_id] = self.single_param_to_pyinquirer(param_id, param_obj, print_help=False)

        # Go through input params and sanitise
        for params in [self.nxf_flags, self.schema_obj.input_params]:
            for param_id in list(params.keys()):
                # Remove if an empty string
                if str(params[param_id]).strip() == '':
                    del params[param_id]
                # Run filter function on value
                filter_func = pyinquirer_objects.get(param_id, {}).get('filter')
                if filter_func is not None:
                    params[param_id] = filter_func(params[param_id])

    def prompt_schema(self):
        """ Go through the pipeline schema and prompt user to change defaults """
        answers = {}
        for param_id, param_obj in self.schema_obj.schema['properties'].items():
            if(param_obj['type'] == 'object'):
                if not param_obj.get('hidden', False) or self.show_hidden:
                    answers.update(self.prompt_group(param_id, param_obj))
            else:
                if not param_obj.get('hidden', False) or self.show_hidden:
                    is_required = param_id in self.schema_obj.schema.get('required', [])
                    answers.update(self.prompt_param(param_id, param_obj, is_required, answers))

        # Split answers into core nextflow options and params
        for key, answer in answers.items():
            if key == 'Nextflow command-line flags':
                continue
            elif key in self.nxf_flag_schema['Nextflow command-line flags']['properties']:
                self.nxf_flags[key] = answer
            else:
                self.params_user[key] = answer

        # Update schema with user params
        self.schema_obj.input_params.update(self.params_user)

    def prompt_param(self, param_id, param_obj, is_required, answers):
        """Prompt for a single parameter"""

        # Print the question
        question = self.single_param_to_pyinquirer(param_id, param_obj, answers)
        answer = PyInquirer.prompt([question], raise_keyboard_interrupt=True)

        # If required and got an empty reponse, ask again
        while type(answer[param_id]) is str and answer[param_id].strip() == '' and is_required:
            click.secho("Error - this property is required.", fg='red', err=True)
            answer = PyInquirer.prompt([question], raise_keyboard_interrupt=True)

        # Don't return empty answers
        if answer[param_id] == '':
            return {}
        return answer

    def prompt_group(self, param_id, param_obj):
        """Prompt for edits to a group of parameters
        Only works for single-level groups (no nested!)

        Args:
          param_id: Paramater ID (string)
          param_obj: JSON Schema keys - no objects (dict)

        Returns:
          Dict of param_id:val answers
        """
        question = {
            'type': 'list',
            'name': param_id,
            'message': param_id,
            'choices': [
                'Continue >>',
                PyInquirer.Separator()
            ]
        }

        for child_param, child_param_obj in param_obj['properties'].items():
            if(child_param_obj['type'] == 'object'):
                logging.error("nf-core only supports groups 1-level deep")
                return {}
            else:
                if not child_param_obj.get('hidden', False) or self.show_hidden:
                    question['choices'].append(child_param)

        # Skip if all questions hidden
        if len(question['choices']) == 2:
            return {}

        while_break = False
        answers = {}
        while not while_break:
            self.print_param_header(param_id, param_obj)
            answer = PyInquirer.prompt([question], raise_keyboard_interrupt=True)
            if answer[param_id] == 'Continue >>':
                while_break = True
                # Check if there are any required parameters that don't have answers
                if self.schema_obj is not None and param_id in self.schema_obj.schema['properties']:
                    for p_required in self.schema_obj.schema['properties'][param_id].get('required', []):
                        req_default = self.schema_obj.input_params.get(p_required, '')
                        req_answer = answers.get(p_required, '')
                        if req_default == '' and req_answer == '':
                            click.secho("Error - '{}' is required.".format(p_required), fg='red', err=True)
                            while_break = False
            else:
                child_param = answer[param_id]
                is_required = child_param in param_obj.get('required', [])
                answers.update(self.prompt_param(child_param, param_obj['properties'][child_param], is_required, answers))

        return answers

    def single_param_to_pyinquirer(self, param_id, param_obj, answers=None, print_help=True):
        """Convert a JSONSchema param to a PyInquirer question

        Args:
          param_id: Paramater ID (string)
          param_obj: JSON Schema keys - no objects (dict)

        Returns:
          Single PyInquirer dict, to be appended to questions list
        """
        if answers is None:
            answers = {}

        question = {
            'type': 'input',
            'name': param_id,
            'message': param_id
        }

        # Print the name, description & help text
        if print_help:
            nice_param_id = '--{}'.format(param_id) if not param_id.startswith('-') else param_id
            self.print_param_header(nice_param_id, param_obj)

        if param_obj.get('type') == 'boolean':
            question['type'] = 'list'
            question['choices'] = ['True', 'False']
            question['default'] = 'False'

        # Start with the default from the param object
        if 'default' in param_obj:
            # Boolean default is cast back to a string later - this just normalises all inputs
            if param_obj['type'] == 'boolean' and type(param_obj['default']) is str:
                question['default'] = param_obj['default'].lower() == 'true'
            else:
                question['default'] = param_obj['default']

        # Overwrite default with parsed schema, includes --params-in etc
        if self.schema_obj is not None and param_id in self.schema_obj.input_params:
            if param_obj['type'] == 'boolean' and type(self.schema_obj.input_params[param_id]) is str:
                question['default'] = 'true' == self.schema_obj.input_params[param_id].lower()
            else:
                question['default'] = self.schema_obj.input_params[param_id]

        # Overwrite default if already had an answer
        if param_id in answers:
            question['default'] = answers[param_id]

        # Coerce default to a string
        if 'default' in question:
            question['default'] = str(question['default'])

        if param_obj.get('type') == 'boolean':
            # Filter returned value
            def filter_boolean(val):
                return val.lower() == 'true'
            question['filter'] = filter_boolean

        if param_obj.get('type') == 'number':
            # Validate number type
            def validate_number(val):
                try:
                    if val.strip() == '':
                        return True
                    float(val)
                except (ValueError):
                    return "Must be a number"
                else:
                    return True
            question['validate'] = validate_number

            # Filter returned value
            def filter_number(val):
                if val.strip() == '':
                    return ''
                return float(val)
            question['filter'] = filter_number

        if param_obj.get('type') == 'integer':
            # Validate integer type
            def validate_integer(val):
                try:
                    if val.strip() == '':
                        return True
                    assert int(val) == float(val)
                except (AssertionError, ValueError):
                    return "Must be an integer"
                else:
                    return True
            question['validate'] = validate_integer

            # Filter returned value
            def filter_integer(val):
                if val.strip() == '':
                    return ''
                return int(val)
            question['filter'] = filter_integer

        if param_obj.get('type') == 'range':
            # Validate range type
            def validate_range(val):
                try:
                    if val.strip() == '':
                        return True
                    fval = float(val)
                    if 'minimum' in param_obj and fval < float(param_obj['minimum']):
                        return "Must be greater than or equal to {}".format(param_obj['minimum'])
                    if 'maximum' in param_obj and fval > float(param_obj['maximum']):
                        return "Must be less than or equal to {}".format(param_obj['maximum'])
                    return True
                except (ValueError):
                    return "Must be a number"
            question['validate'] = validate_range

            # Filter returned value
            def filter_range(val):
                if val.strip() == '':
                    return ''
                return float(val)
            question['filter'] = filter_range

        if 'enum' in param_obj:
            # Use a selection list instead of free text input
            question['type'] = 'list'
            question['choices'] = param_obj['enum']

            # Validate enum from schema
            def validate_enum(val):
                if val == '':
                    return True
                if val in param_obj['enum']:
                    return True
                return "Must be one of: {}".format(", ".join(param_obj['enum']))
            question['validate'] = validate_enum

        # Validate pattern from schema
        if 'pattern' in param_obj:
            def validate_pattern(val):
                if val == '':
                    return True
                if re.search(param_obj['pattern'], val) is not None:
                    return True
                return "Must match pattern: {}".format(param_obj['pattern'])
            question['validate'] = validate_pattern

        return question

    def print_param_header(self, param_id, param_obj):
        if 'description' not in param_obj and 'help_text' not in param_obj:
            return
        header_str = click.style(param_id, bold=True)
        if 'description' in param_obj:
            header_str += ' - {}'.format(param_obj['description'])
        if 'help_text' in param_obj:
            # Strip indented and trailing whitespace
            help_text = textwrap.dedent(param_obj['help_text']).strip()
            # Replace single newlines, leave double newlines in place
            help_text = re.sub(r'(?<!\n)\n(?!\n)', ' ', help_text)
            header_str += "\n" + click.style(help_text, dim=True)
        click.echo("\n"+header_str, err=True)

    def strip_default_params(self):
        """ Strip parameters if they have not changed from the default """

        # Schema defaults
        for param_id, val in self.schema_obj.schema_defaults.items():
            if self.schema_obj.input_params.get(param_id) == val:
                del self.schema_obj.input_params[param_id]

        # Nextflow flag defaults
        for param_id, val in self.nxf_flag_schema['Nextflow command-line flags']['properties'].items():
            if param_id in self.nxf_flags and self.nxf_flags[param_id] == val.get('default'):
                del self.nxf_flags[param_id]

    def build_command(self):
        """ Build the nextflow run command based on what we know """

        # Core nextflow options
        for flag, val in self.nxf_flags.items():
            # Boolean flags like -resume
            if isinstance(val, bool) and val:
                self.nextflow_cmd += " {}".format(flag)
            # String values
            elif not isinstance(val, bool):
                self.nextflow_cmd += ' {} "{}"'.format(flag, val.replace('"', '\\"'))

        # Pipeline parameters
        if len(self.schema_obj.input_params) > 0:

            # Write the user selection to a file and run nextflow with that
            if self.use_params_file:
                with open(self.params_out, "w") as fp:
                    json.dump(self.schema_obj.input_params, fp, indent=4)
                self.nextflow_cmd += ' {} "{}"'.format("-params-file", os.path.relpath(self.params_out))

            # Call nextflow with a list of command line flags
            else:
                for param, val in self.schema_obj.input_params.items():
                    # Boolean flags like --saveTrimmed
                    if isinstance(val, bool) and val:
                        self.nextflow_cmd += " --{}".format(param)
                    # everything else
                    else:
                        self.nextflow_cmd += ' --{} "{}"'.format(param, str(val).replace('"', '\\"'))


    def launch_workflow(self):
        """ Launch nextflow if required  """
        intro = click.style("Nextflow command:", bold=True, underline=True)
        cmd = click.style(self.nextflow_cmd, fg='magenta')
        logging.info("{}\n  {}\n\n".format(intro, cmd))

        if click.confirm('Do you want to run this command now? '+click.style('[y/N]', fg='green'), default=False, show_default=False):
            logging.info("Launching workflow!")
            subprocess.call(self.nextflow_cmd, shell=True)
