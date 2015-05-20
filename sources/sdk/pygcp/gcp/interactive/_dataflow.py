# Copyright 2014 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Google Cloud Platform library - Dataflow IPython Functionality."""

import inspect as _inspect
import json as _json
import sys as _sys
import IPython as _ipython
import IPython.core.magic as _magic
from ._commands import CommandParser as _CommandParser
from ._html import Html as _Html

import gcp.dataflow as df


class DataflowDataCollector(df.pipeline.PipelineVisitor):

  def __init__(self):
    self._data = dict()

  def visit_value(self, value, producer_node):
    if type(value) == df.pvalue.PCollection:
      items = list(value.get())
      collection = {
        'count': len(items),
        'data': items[:25]
      }

      self._data[producer_node.full_label] = collection

  def visit(self, pipeline):
    pipeline.visit(self)
    return self._data


class DataflowGraphBuilder(df.pipeline.PipelineVisitor):

  def __init__(self):
    self._nodes = list()
    self._node_stack = list()
    self._node_map = dict()

  def add_node(self, transform_node):
    label = transform_node.full_label
    name = label

    slash_index = name.rfind('/')
    if slash_index > 0:
      name = name[slash_index + 1:]

    graph_node = {
      'id': label,
      'name': name,
      'nodes': [],
      'edges': []
    }

    self._node_map[label] = graph_node

    if len(self._node_stack) == 0:
      self._nodes.append(graph_node)
    else:
      self._node_stack[-1].get('nodes').append(graph_node)

    for input_value in transform_node.inputs:
      parent_node = self._node_map[input_value.producer.full_label]
      parent_node.get('edges').append(label)

    return graph_node

  def enter_composite_transform(self, transform_node):
    if len(transform_node.full_label) == 0:
      # Ignore the root node representing the pipeline itself
      return

    graph_node = self.add_node(transform_node)
    self._node_stack.append(graph_node)

  def leave_composite_transform(self, transform_node):
    if len(transform_node.full_label) == 0:
      # Ignore the root node representing the pipeline itself
      return

    self._node_stack.pop()

  def visit_transform(self, transform_node):
    self.add_node(transform_node)

  def visit(self, pipeline):
    pipeline.visit(self)
    return self._nodes


class DataflowLocalCatalog(object):

  def __init__(self, catalog=None, ns=None, args=None):
    self.sources = dict()
    self.sinks = dict()

    if catalog is not None:
      for name in catalog.sources.names:
        source = catalog.sources.get(name)
        if callable(source):
          source = source(args)
        self.add_source(name, source, ns)

      for name in catalog.sinks.names:
        self.add_sink(name, ns)

  def add_sink(self, name, ns):
    data = ns[name] = list()
    self.sinks[name] = DataflowLocalCatalog.ListSink(data)

  def add_source(self, name, source, ns):
    data = ns.get(name, None)
    if data is not None:
      if type(data) != list:
        raise TypeError('"%s" does not represent a list' % name)
      source = DataflowLocalCatalog.ListSource(data)

    self.sources[name] = source

  class ListSource(df.io.iobase.Source):

    def __init__(self, data):
      self._data = data

    def reader(self):
      return DataflowLocalCatalog.ListSource.Reader(self._data)

    class Reader(df.io.iobase.SourceReader):

      def __init__(self, data):
        self._data = data

      def __enter__(self):
        return self

      def __exit__(self, exception_type, exception_value, traceback):
        pass

      def __iter__(self):
        return self._data.__iter__()

  class ListSink(df.io.iobase.Sink):

    def __init__(self, data):
      self._data = data

    def writer(self):
      return DataflowLocalCatalog.ListSink.Writer(self._data)

    class Writer(df.io.iobase.SinkWriter):

      def __init__(self, data):
        self._data = data

      def __enter__(self):
        return self

      def __exit__(self, exception_type, exception_value, traceback):
        pass

      def Write(self, o):
        self._data.append(o)


class DataflowJSONEncoder(_json.JSONEncoder):

  def default(self, obj):
    if isinstance(obj, df.window.BoundedWindow):
      return str(obj)
    else:
      return super(DataflowJSONEncoder, self).default(obj)


class DataflowExecutor(object):

  def __init__(self, dataflow_method, ns):
    self._create_dataflow = dataflow_method
    self._ns = ns

    self._catalog = dataflow_method.catalog if hasattr(dataflow_method, 'catalog') else None
    self._args = dataflow_method.args if hasattr(dataflow_method, 'args') else None

  def execute(self, command_line):
    parser = _CommandParser.create('dataflow')

    run_parser = parser.subcommand('run', self._run, 'runs the dataflow')
    run_parser.add_argument('--execution', choices=['local', 'remote'], default='local',
                            help='whether the dataflow should be executed locally or remotely')
    if self._args is not None:
      for args, kwargs in self._args:
        run_parser.add_argument(*args, **kwargs)

    args = parser.parse(command_line)
    if args is not None:
      return args.func(args)

  @staticmethod
  def from_namespace(ns):
    module = ns.get('dataflow', None)
    if module is None:
      raise Exception('A module named "dataflow" was not found in this notebook.')

    dataflow_method = module.__dict__.get('dataflow', None)
    if dataflow_method is None:
      dataflow_method = module.__dict__.get('main', None)
    if ((dataflow_method is None) or not callable(dataflow_method) or
        (len(_inspect.getargspec(dataflow_method)[0]) != 3)):
      raise Exception('The dataflow module defined does not contain a ' +
                      '"dataflow(pipeline, catalog, args)" method.')

    return DataflowExecutor(dataflow_method, ns)

  def _run(self, args):
    args = vars(args)

    runner = df.runners.DirectPipelineRunner()
    pipeline = df.Pipeline(runner)

    self._create_dataflow(pipeline,
                          DataflowLocalCatalog(self._catalog, self._ns, args),
                          args)
    pipeline.run()
    return pipeline


class PTransformExecutor(object):

  def __init__(self, cls, ns):
    self._cls = cls
    self._ns = ns

  def execute(self, input_name, output_name):
    catalog = DataflowLocalCatalog()
    catalog.add_source(input_name, None, self._ns)
    if output_name is not None:
      catalog.add_sink(output_name, self._ns)

    runner = df.runners.DirectPipelineRunner()
    pipeline = df.Pipeline(runner)

    input_collection = pipeline.read('read', catalog.sources[input_name])
    output_collection = input_collection | self._cls(self._cls.__name__)
    if output_name is not None:
      output_collection.write('write', catalog.sinks[output_name])

    pipeline.run()
    return pipeline

  @staticmethod
  def from_namespace(ns, name):
    module = ns.get('dataflow', None)
    if module is None:
      raise Exception('A module named "dataflow" was not found in this notebook.')

    transform_class = module.__dict__.get(name, None)
    if (transform_class is None) or type(transform_class) != type:
      raise Exception('The dataflow module does not contain a class named "%s"' % name)
    if not issubclass(transform_class, df.PTransform):
      raise Exception('The class named "%s" does not inherit from PTransform.')

    return PTransformExecutor(transform_class, ns)


@_magic.register_line_magic
def dataflow(line):
  try:
    dataflow_executor = DataflowExecutor.from_namespace(_ipython.get_ipython().user_ns)
    return dataflow_executor.execute(line)
  except Exception as e:
    _sys.stderr.write(e.message)
    return None


@_magic.register_line_magic
def ptransform(line):
  parser = _CommandParser.create('ptransform')

  run_parser = parser.subcommand('run', None, 'runs the specified PTransform')
  run_parser.add_argument('--name', required=True, metavar='class',
                          help='the name of the PTransform class to run')
  run_parser.add_argument('--input', required=True, metavar='variable',
                          help='the name of the variable containing the input list')
  run_parser.add_argument('--output', metavar='variable',
                          help='the name of the variable to create for the output list')

  args = parser.parse(line)
  if args is None:
    return

  try:
    transform_executor = PTransformExecutor.from_namespace(_ipython.get_ipython().user_ns,
                                                           args.name)
    return transform_executor.execute(args.input, args.output)
  except Exception as e:
    _sys.stderr.write(e.message)
    return None


def _pipeline_repr_html_(self):
  graph = _json.dumps(DataflowGraphBuilder().visit(self))
  data = _json.dumps(DataflowDataCollector().visit(self), cls=DataflowJSONEncoder)

  # Markup consists of an <svg> element for graph rendering, a <label> element
  # for describing the selected graph node, and a <div> to contain a table
  # rendering of the selected node's output.
  markup = """
    <svg class="df-pipeline"><g /></svg>
    <label class="df-title"></label>
    <div class="df-data"></div>
    """
  html = _Html(markup)
  html.add_class('df-run')
  html.add_dependency('style!/static/extensions/dataflow.css', 'css')
  html.add_dependency('extensions/dataflow', 'dataflow')
  html.add_script('dataflow.renderPipeline(dom, %s, %s)' % (graph, data))

  return html._repr_html_()

def _pipeline_repr_str_(self):
  return ''

df.Pipeline._repr_html_ = _pipeline_repr_html_
df.Pipeline.__repr__ = _pipeline_repr_str_
df.Pipeline.__str__ = _pipeline_repr_str_