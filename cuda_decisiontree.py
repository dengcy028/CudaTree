import pycuda.autoinit
import pycuda.driver as cuda
from pycuda import gpuarray
from pycuda.compiler import SourceModule
from sklearn import tree
import sklearn.datasets
from sklearn.datasets import load_iris
import numpy as np
import pycuda.autoinit
import pycuda.driver as cuda
from pycuda import gpuarray
from pycuda.compiler import SourceModule
import math
import sys

def mk_kernel(n_samples, n_labels, kernel_file,  _cache = {}):
  key = (n_samples, n_labels)
  if key in _cache:
    return _cache[key]
  
  with open(kernel_file) as code_file:
    code = code_file.read()  
    mod = SourceModule(code % (n_samples, n_labels))
    fn = mod.get_function("compute")
    _cache[key] = fn
    return fn

def mk_scan_kernel(n_samples, n_labels, threads_per_block, kernel_file,  _cache = {}):
  key = (n_samples, n_labels)
  if key in _cache:
    return _cache[key]
  
  with open(kernel_file) as code_file:
    code = code_file.read()  
    mod = SourceModule(code % (n_samples, n_labels, threads_per_block))
    fn = mod.get_function("prefix_scan")
    _cache[key] = fn
    return fn


class Node(object):
  def __init__(self):
    self.value = None 
    self.error = None
    self.samples = None
    self.feature_threshold = None
    self.feature_index = None
    self.left_child = None
    self.right_child = None
    self.height = None


class DecisionTree(object): 
  COMPUTE_KERNEL_SS = "comput_kernel_ss.cu"   #One thread per feature.
  COMPUTE_KERNEL_PS = "comput_kernel_ps.cu"   #One block per feature.
  COMPUTE_KERNEL_PP = "comput_kernel_pp.cu"   #Based on kernel 2, add parallel reduction to find minimum impurity.
  COMPUTE_KERNEL_CP = "comput_kernel_cp.cu"   #Based on kernel 3, utilized the coalesced memory access.
  SCAN_KERNEL_S = "scan_kernel_s.cu"          #Serialized prefix scan.
  SCAN_KERNEL_P = "scan_kernel_p.cu"          #Simple parallel prefix scan.

  COMPT_THREADS_PER_BLOCK = 64  #The number of threads do computation per block.
  SCAN_THREADS_PER_BLOCK = 64   #The number of threads do prefix scan per block.

  def __init__(self):
    self.root = None
    self.compt_kernel_type = None
    self.num_labels = None

  def fit(self, samples, target, scan_kernel_type, compt_kernel_type):
    assert isinstance(samples, np.ndarray)
    assert isinstance(target, np.ndarray)
    assert samples.size / samples[0].size == target.size
    
    self.num_labels = np.unique(target).size
    self.kernel = mk_kernel(target.size, self.num_labels, compt_kernel_type)
    self.scan_kernel = mk_scan_kernel(target.size, self.num_labels, self.COMPT_THREADS_PER_BLOCK, scan_kernel_type)
    
    self.compt_kernel_type = compt_kernel_type
    samples = np.require(np.transpose(samples), dtype = np.float32, requirements = 'C')
    target = np.require(np.transpose(target), dtype = np.int32, requirements = 'C') 
    self.root = self.__construct(samples, target, 1, 1.0) 


  def __construct(self, samples, target, Height, error_rate):
    def check_terminate():
      if error_rate == 0:
        return True
      else:
        return False
    
    ret_node = Node()
    ret_node.error = error_rate
    ret_node.samples = target.size
    ret_node.height = Height

    if check_terminate():
      ret_node.value = target[0]
      return ret_node

    sorted_examples = np.empty_like(samples)
    sorted_targets = np.empty_like(samples).astype(np.int32)
    sorted_targetsGPU = None 

    for i,f in enumerate(samples):
      sorted_index = np.argsort(f)
      sorted_examples[i] = samples[i][sorted_index]
      sorted_targets[i] = target[sorted_index]
   
    sorted_targetsGPU = gpuarray.to_gpu(sorted_targets)
    sorted_examplesGPU = gpuarray.to_gpu(sorted_examples)
    n_features = sorted_targetsGPU.shape[0]
    impurity_left = gpuarray.empty(n_features, dtype = np.float32)
    impurity_right = gpuarray.empty(n_features, dtype = np.float32)
    min_split = gpuarray.empty(n_features, dtype = np.int32)

    
    n_features = sorted_targetsGPU.shape[0]
    n_samples = sorted_targetsGPU.shape[1]
    leading = sorted_targetsGPU.strides[0] / target.itemsize

    assert n_samples == leading #Just curious about if they can be different.
    
    grid = (n_features, 1) 
    label_count = gpuarray.empty(ret_node.samples * self.num_labels * n_features, dtype = np.int32)
    
    if self.compt_kernel_type !=  self.COMPUTE_KERNEL_SS:
      block = (self.COMPT_THREADS_PER_BLOCK, 1, 1)
    else:
      block = (1, 1, 1)
    
    self.scan_kernel(sorted_targetsGPU, 
                label_count,
                np.int32(n_features), 
                np.int32(n_samples), 
                np.int32(leading),
                block = (self.SCAN_THREADS_PER_BLOCK, 1, 1),
                grid = grid)
    
    self.kernel(sorted_examplesGPU,
                impurity_left,
                impurity_right,
                label_count,
                min_split,
                np.int32(n_features), 
                np.int32(n_samples), 
                np.int32(leading),
                block = block,
                grid = grid)

    imp_left = impurity_left.get()
    imp_right = impurity_right.get()
    imp_total = imp_left + imp_right
 
    ret_node.feature_index =  imp_total.argmin()
    row = ret_node.feature_index
    col = min_split.get()[row]
    ret_node.feature_threshold = (sorted_examples[row][col] + sorted_examples[row][col + 1]) / 2.0 

    boolean_mask_left = (samples[ret_node.feature_index] < ret_node.feature_threshold)
    boolean_mask_right = (samples[ret_node.feature_index] >= ret_node.feature_threshold)
    data_left =  samples[:, boolean_mask_left].copy()
    target_left = target[boolean_mask_left].copy()
    assert len(target_left) > 0
    ret_node.left_child = self.__construct(data_left, target_left, Height + 1, imp_left[ret_node.feature_index])

    data_right = samples[:, boolean_mask_right].copy()
    target_right = target[boolean_mask_right].copy()
    assert len(target_right) > 0 
    ret_node.right_child = self.__construct(data_right, target_right, Height + 1, imp_right[ret_node.feature_index])
    
    return ret_node 

   
  def __predict(self, val):
    temp = self.root
    while True:
      if temp.left_child and temp.right_child:
        if val[temp.feature_index] < temp.feature_threshold:
          temp = temp.left_child
        else:
          temp = temp.right_child
      else: 
          return temp.value

  def predict(self, inputs):
    res = []
    for val in inputs:
      res.append(self.__predict(val))
    return np.array(res)

  def print_tree(self):
    def recursive_print(node):
      if node.left_child and node.right_child:
        print "Height : %s,  Feature Index : %s,  Threshold : %s Samples: %s" % (node.height, node.feature_index, node.feature_threshold, node.samples)  
        recursive_print(node.left_child)
        recursive_print(node.right_child)
      else:
        print "Leaf Height : %s,  Samples : %s" % (node.height, node.samples)  
    assert self.root is not None
    recursive_print(self.root)


if __name__ == "__main__":
  d = DecisionTree()  
  dataset = sklearn.datasets.load_digits()
  #num_labels = len(dataset.target_names)  
  import cProfile 
  print "begin"
  #cProfile.run("d.fit(dataset.data, dataset.target, DecisionTree.COMPUTE_KERNEL_PP, DecisionTree.SCAN_KERNEL_P)")d.fit(dataset.data, dataset.target, DecisionTree.SCAN_KERNEL_P, DecisionTree.COMPUTE_KERNEL_cp)
  d.fit(dataset.data, dataset.target, DecisionTree.SCAN_KERNEL_P, DecisionTree.COMPUTE_KERNEL_PS)
  d.print_tree()

  '''
  res = d.predict(dataset.data)
  print np.allclose(dataset.target, res)
  '''

