"""Provides neural-network specific Ops.

:note: TODO: factor this out into a neural-network toolbox.
"""

from theano import gof
from theano import scalar
from theano import printing
from theano.printing import pprint
from theano.tensor import basic as tensor
from theano.tensor import elemwise
from theano.tensor import opt
from theano.compile import optdb
import numpy

############
#
# SCALAR OPS
#

class ScalarSigmoid(scalar.UnaryScalarOp):
    @staticmethod
    def st_impl(x):
        if x < -30.0:
            return 0.0
        if x > 30.0:
            return 1.0 
        return 1.0 / (1.0 + numpy.exp(-x))
    def impl(self, x):
        return ScalarSigmoid.st_impl(x)
    def grad(self, (x,), (gz,)):
        y = scalar_sigmoid(x)
        return [gz * y * (1.0 - y)]
    def c_code(self, node, name, (x,), (z,), sub):
        if node.inputs[0].type == scalar.float32:
            # These constants were obtained by looking at the output of python commands like:
            #  for i in xrange(750):
            #      print i, repr( theano._asarray(1.0, dtype=dt) / (theano._asarray(1.0, dtype=dt) + numpy.exp(-theano._asarray([i,-i], dtype=dt))))
            # the boundary checks prevent us from generating inf
            return """%(z)s = %(x)s < -88.0f ? 0.0 : %(x)s > 15.0f ? 1.0f : 1.0f /(1.0f + exp(-%(x)s));""" % locals()
        elif node.inputs[0].type == scalar.float64:
            return """%(z)s = %(x)s < -709.0 ? 0.0 : %(x)s > 19.0 ? 1.0 : 1.0 /(1.0+exp(-%(x)s));""" % locals()
        else:
            raise NotImplementedError('only floatingpoint is implemented')
    def c_code_cache_version(self):
        v = super(ScalarSigmoid, self).c_code_cache_version()
        if v:
            return (2,) + v
        else:
            return v
scalar_sigmoid = ScalarSigmoid(scalar.upgrade_to_float, name='scalar_sigmoid')
sigmoid = elemwise.Elemwise(scalar_sigmoid, name='sigmoid')

pprint.assign(sigmoid, printing.FunctionPrinter('sigmoid'))


class ScalarSoftplus(scalar.UnaryScalarOp):
    @staticmethod
    def static_impl(x):
        if x < -30.0:
            return 0.0
        if x > 30.0:
            return x
        return numpy.log1p(numpy.exp(x))
    def impl(self, x):
        return ScalarSoftplus.static_impl(x)
    def grad(self, (x,), (gz,)):
        return [gz * scalar_sigmoid(x)]
    def c_code(self, node, name, (x,), (z,), sub):
        if node.inputs[0].type == scalar.float32:
            # These constants were obtained by looking at the output of python commands like:
            #  for i in xrange(750):
            #      print i, repr( numpy.log1p(numpy.exp(theano._asarray([i,-i], dtype=dt))))
            # the boundary checks prevent us from generating inf
            return """%(z)s = %(x)s < -103.0f ? 0.0 : %(x)s > 14.0f ? %(x)s : log1p(exp(%(x)s));""" % locals()
        elif node.inputs[0].type == scalar.float64:
            return """%(z)s = %(x)s < -745.0 ? 0.0 : %(x)s > 16.0 ? %(x)s : log1p(exp(%(x)s));""" % locals()
        else:
            raise NotImplementedError('only floatingpoint is implemented')
    def c_code_cache_version(self):
        v = super(ScalarSoftplus, self).c_code_cache_version()
        if v:
            return (2,) + v
        else:
            return v
scalar_softplus = ScalarSoftplus(scalar.upgrade_to_float, name='scalar_softplus')
softplus = elemwise.Elemwise(scalar_softplus, name='softplus')

pprint.assign(softplus, printing.FunctionPrinter('softplus'))


############
#
# TENSOR OPS
#

class SoftmaxWithBias(gof.Op):
    """
    An L{Op} for the output of neural-net multiclass classifiers.

    @type x: is a matrix of floats (32 or 64)
    @type b: is a [row] vector of floats (32 or 64), length is number of cols in x

    This L{Op}'s output is softmax(x+b).
    softmax(x[i]) is the i'th distribution over len(x[i]) options.
    """

    nin = 2
    nout = 1
    def __init__(self, **kwargs):
        gof.Op.__init__(self, **kwargs)

    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return tensor.hashtype(self)

    def make_node(self, x, b):
        x = tensor.as_tensor_variable(x)
        b = tensor.as_tensor_variable(b)
        if x.type.ndim != 2 \
                or x.type.dtype not in ['float32', 'float64']:
            raise ValueError('x must be 2-d tensor of floats')
        if b.type.ndim != 1 \
                or x.type.dtype not in ['float32', 'float64']:
            raise ValueError('b must be 1-d tensor of floats')

        sm = x.type.make_variable()
        return gof.Apply(self, [x, b], [sm])

    def perform(self, node, input_storage, output_storage):
        x, b = input_storage
        if b.shape[0] != x.shape[1]:
            raise ValueError('b must have same number of columns as x')

        sm = numpy.zeros_like(x)
        for i in xrange(sm.shape[0]):
            row = x[i] + b
            sm[i] = numpy.exp(row - numpy.max(row))
            sm[i] *= 1.0 / numpy.sum(sm[i])
        output_storage[0][0] = sm

    def grad(self, (x, b), (g_sm,)):
        sm = softmax_with_bias(x, b)
        dx = softmax_grad(g_sm, sm)
        db = tensor.sum(dx, axis = 0)
        return dx, db

    def c_headers(self):
        return ['<iostream>','<cmath>']

    @staticmethod
    def c_code_template():
        # this implementation was lifted from
        # /u/bergstrj/cvs/bergstrj/src/feb07/nn.cxx

        #TODO: put this into a templated function, in the support code
        #TODO: declare the max of each row as an Op output

        #TODO: set error messages for failures in this code

        #TODO: use this to accept float32 and int32: node.inputs[0].type.dtype_specs()[1]
        init_decl = """
        npy_intp* Nx = %(x)s->dimensions;

        if (%(x)s->nd != 2)
        {
            PyErr_SetString(PyExc_ValueError, "a not 2d tensor");
            %(fail)s;
        }
        if (%(b)s->nd != 1)
        {
            PyErr_SetString(PyExc_ValueError, "b not 1d tensor");
            %(fail)s;
        }
        if ((%(x)s->descr->type_num != PyArray_DOUBLE)&&(%(x)s->descr->type_num != PyArray_FLOAT))
        {
            PyErr_SetString(PyExc_TypeError, "a not float");
            %(fail)s;
        }
        if ((%(b)s->descr->type_num != PyArray_DOUBLE) && (%(b)s->descr->type_num != PyArray_FLOAT))
        {
            PyErr_SetString(PyExc_TypeError, "b not float");
            %(fail)s;
        }
        if ((%(x)s->dimensions[1] != %(b)s->dimensions[0]))
        {
            PyErr_Format(PyExc_ValueError, "number of columns in x (%%zi) does not match length of b (%%zi)",
            %(x)s->dimensions[1], %(b)s->dimensions[0]);
            %(fail)s;
        }

        if ((NULL == %(sm)s)
            || (%(sm)s->dimensions[0] != %(x)s->dimensions[0])
            || (%(sm)s->dimensions[1] != %(x)s->dimensions[1]))
        {
            if (NULL != %(sm)s) Py_XDECREF(%(sm)s);
            %(sm)s = (PyArrayObject*)PyArray_SimpleNew(2, PyArray_DIMS(%(x)s), type_num_%(x)s);
            if(!%(sm)s) {
                PyErr_SetString(PyExc_MemoryError, "failed to alloc sm output");
                %(fail)s
            }
        }
        """

        begin_row_loop = """
        for (size_t i = 0; i < Nx[0]; ++i)
        {
            size_t j;
            double sum = 0.0;
            bool  discount_max = false;

            const dtype_%(x)s* __restrict__ x_i = (dtype_%(x)s*)(%(x)s->data + %(x)s->strides[0] * i);
            const dtype_%(b)s* __restrict__ b_i = (dtype_%(b)s*)(%(b)s->data);
            dtype_%(sm)s* __restrict__ sm_i = (dtype_%(sm)s*)(%(sm)s->data + %(sm)s->strides[0] * i);
        """

        inside_row_loop = """
            npy_intp Sx = %(x)s->strides[1]/sizeof(dtype_%(x)s);
            npy_intp Sb = %(b)s->strides[0]/sizeof(dtype_%(b)s);
            npy_intp Ssm = %(sm)s->strides[1]/sizeof(dtype_%(sm)s);

            size_t row_max_j=0;
            dtype_%(sm)s row_max = x_i[0] + b_i[0];
            //std::cout << "0 " << row_max << "\\n";
            // Get the maximum value of the row
            for (j = 1; j < Nx[1]; ++j)
            {
                dtype_%(sm)s row_ij = x_i[j * Sx] +  b_i[j * Sb];
                //std::cout << "1 " << row_ij << "\\n";
                row_max_j = (row_ij > row_max) ? j : row_max_j;
                row_max   = (row_ij > row_max) ? row_ij : row_max;
            }

            for (j = 0; j < Nx[1]; ++j)
            {
                dtype_%(sm)s row_ij = x_i[j * Sx] +  b_i[j * Sb];
                //std::cout << "2 " << j << " " << row_ij << " " << row_max << "\\n";
                dtype_%(sm)s sm_ij = exp(row_ij - row_max);
                //std::cout << "3 " << j << " " << sm_ij << "\\n";
                sum += sm_ij;
                sm_i[j * Ssm] = sm_ij;
            }

            //cblas_dscal(x.N, 1.0 / sum, &mat_at(s,i,0), s.n);
            double sum_inv = 1.0 / sum;
            for (j = 0; j < Nx[1]; ++j)
            {
                sm_i[j * Ssm] *= sum_inv;
            }

        """

        end_row_loop = """
        }
        """

        return (init_decl, begin_row_loop, inside_row_loop, end_row_loop)


    def c_code(self, node, name, (x, b), (sm,), sub):
        code_template = ''.join(self.c_code_template())
        return code_template % dict(locals(), **sub)

    @staticmethod
    def c_code_cache_version():
        return (5,)

softmax_with_bias = SoftmaxWithBias()



class SoftmaxGrad(gof.Op):
    """Gradient wrt x of the Softmax Op"""
    nin = 2
    nout = 1

    def __init__(self, **kwargs):
        gof.Op.__init__(self, **kwargs)

    def __eq__(self, other):
        return type(self) == type(other)

    def __hash__(self):
        return tensor.hashtype(self)

    def make_node(self, dy, sm, **kwargs):
        dy = tensor.as_tensor_variable(dy)
        sm = tensor.as_tensor_variable(sm)
        return gof.Apply(self, [dy, sm], [sm.type.make_variable()])

    def perform(self, node, input_storage, output_storage):
        dy, sm = input_storage
        dx = numpy.zeros_like(sm)
        #dx[i,j] = - (\sum_k dy[i,k] sm[i,k]) sm[i,j] + dy[i,j] sm[i,j]
        for i in xrange(sm.shape[0]):
            dy_times_sm_i = dy[i] * sm[i]
            dx[i] = dy_times_sm_i - sum(dy_times_sm_i) * sm[i]
        output_storage[0][0] = dx

    def grad(self, *args):
        raise NotImplementedError()

    def c_code_cache_version(self):
        return (3,)
    def c_code(self, node, name, (dy, sm), (dx,), sub):
        return '''
        if ((%(dy)s->descr->type_num != PyArray_DOUBLE) && (%(dy)s->descr->type_num != PyArray_FLOAT))
        {
            PyErr_SetString(PyExc_TypeError, "types should be float or float64");
            %(fail)s;
        }
        if ((%(sm)s->descr->type_num != PyArray_DOUBLE) && (%(sm)s->descr->type_num != PyArray_FLOAT))
        {
            PyErr_SetString(PyExc_TypeError, "types should be float or float64");
            %(fail)s;
        }
        if ((%(dy)s->nd != 2)
            || (%(sm)s->nd != 2))
        {
            PyErr_SetString(PyExc_ValueError, "rank error");
            %(fail)s;
        }
        if (%(dy)s->dimensions[0] != %(sm)s->dimensions[0])
        {
            PyErr_SetString(PyExc_ValueError, "dy.shape[0] != sm.shape[0]");
            %(fail)s;
        }
        if ((NULL == %(dx)s)
            || (%(dx)s->dimensions[0] != %(sm)s->dimensions[0])
            || (%(dx)s->dimensions[1] != %(sm)s->dimensions[1]))
        {
            Py_XDECREF(%(dx)s);
            %(dx)s = (PyArrayObject*) PyArray_SimpleNew(2, PyArray_DIMS(%(sm)s),
                                                        type_num_%(sm)s);
            if (!%(dx)s)
            {
                PyErr_SetString(PyExc_MemoryError, "failed to alloc dx output");
                %(fail)s;
            }
        }

        for (size_t i = 0; i < %(dx)s->dimensions[0]; ++i)
        {
            const dtype_%(dy)s* __restrict__ dy_i = (dtype_%(dy)s*) (%(dy)s->data + %(dy)s->strides[0] * i);
            npy_intp Sdy = %(dy)s->strides[1]/sizeof(dtype_%(dy)s);
            const dtype_%(sm)s* __restrict__ sm_i = (dtype_%(sm)s*) (%(sm)s->data + %(sm)s->strides[0] * i);
            npy_intp Ssm = %(sm)s->strides[1]/sizeof(dtype_%(sm)s);
            dtype_%(dx)s* __restrict__ dx_i = (dtype_%(dx)s*) (%(dx)s->data + %(dx)s->strides[0] * i);
            npy_intp Sdx = %(dx)s->strides[1]/sizeof(dtype_%(dx)s);

            double sum_dy_times_sm = 0.;
            for (size_t j = 0; j < %(dx)s->dimensions[1]; ++j)
            {
                dx_i[j * Sdx] = dy_i[j * Sdy] * sm_i[j * Ssm];
                sum_dy_times_sm += dx_i[j * Sdx];
            }
            for (size_t j = 0; j < %(dx)s->dimensions[1]; ++j)
            {
                dx_i[j * Sdx] -= sum_dy_times_sm * sm_i[j * Ssm];
            }
        }
        ''' % dict(locals(), **sub)
softmax_grad = SoftmaxGrad()

class Softmax(gof.Op):
    """
    WRITEME
    """

    nin = 1
    nout = 1
    def __init__(self, **kwargs):
        gof.Op.__init__(self, **kwargs)
    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return hash(type(self))

    def make_node(self, x):
        x = tensor.as_tensor_variable(x)
        if x.type.ndim != 2 \
                or x.type.dtype not in ['float32', 'float64']:
            raise ValueError('x must be 2-d tensor of floats')

        sm = x.type.make_variable()
        return gof.Apply(self, [x], [sm])

    def perform(self, node, input_storage, output_storage):
        x, = input_storage
        sm = numpy.zeros_like(x)
        for i in xrange(sm.shape[0]):
            row = x[i]
            sm[i] = numpy.exp(row - numpy.max(row))
            sm[i] /= numpy.sum(sm[i])
        output_storage[0][0] = sm

    def grad(self, (x,), (g_sm,)):
        sm = softmax(x)
        return [softmax_grad(g_sm, sm)]
softmax = Softmax()

@opt.register_specialize
@gof.local_optimizer([softmax])
def local_softmax_with_bias(node):
    """Try to turn softmax(sum_of_stuff) -> softmax_w_bias(matrix, bias)
    """
    if node.op == softmax:
        x, = node.inputs
        if x.owner and x.owner.op == tensor.add:
            vectors = []
            non_vectors = []
            for x_in in x.owner.inputs:
                if list(x_in.type.broadcastable) == [True, False]:
                    # print isinstance(x_in.owner.op, tensor.DimShuffle)
                    #since specialization comes relatively late in optimization, 
                    # we don't want to put in extra DimShuffles un-necessarily.
                    if x_in.owner and isinstance(x_in.owner.op, tensor.DimShuffle)\
                            and list(x_in.owner.inputs[0].type.broadcastable)==[False]:
                        # cut out the DimShuffle that was broadcasting a vector
                        vectors.append(x_in.owner.inputs[0])
                    else:
                        # insert an extra DimShuffle to correct the old one
                        vectors.append(tensor.DimShuffle((True, False), (1,))(x_in))
                else:
                    non_vectors.append(x_in)

            assert non_vectors #not empty
            if vectors:
                #we're in business...
                if len(vectors)>1:
                  vector_sum = tensor.add(*vectors)
                else:
                  vector_sum = vectors[0]

                if len(non_vectors)>1:
                  non_vector_sum = tensor.add(*non_vectors)
                else:
                  non_vector_sum = non_vectors[0]

                try:
                    sm_bias = softmax_with_bias(non_vector_sum, vector_sum)
                except:
                    #if our arguments have the wrong types, then forget about it
                    return
                return [sm_bias]

@gof.local_optimizer([tensor._shape])
def local_shape_lift_softmax(node):
    """shape(softmax(x)) -> shape(x)
    shape(softmax(x+b)) -> shape(x)"""
    if node.op == tensor._shape:
        sm = node.inputs[0]
        if sm.owner and sm.owner in (softmax, softmax_with_bias):
            sm_w_bias = local_softmax_with_bias.transform(sm.owner)
            if sm_w_bias:
                assert sm_w_bias[0].owner.op == softmax_with_bias
                x_var, b_var = sm_w_bias[0].owner.inputs
            else:
                x_var = sm.owner.inputs[0]
            return tensor.shape(x_var),
opt.register_specialize(local_shape_lift_softmax, 'shape_lift')


class CrossentropySoftmaxArgmax1HotWithBias(gof.Op):
    """A special compound L{Op} for the output of neural-net classifiers.

    :type x: is a matrix of floats (32 or 64)
    :type b: is a [row] vector of floats (32 or 64), length is number of cols in x
    :type y_idx: a [column] vector of int (32 or 64), length is number of rows in x

    :returns:  row-wise NLL, softmax(x+b), row-wise argmax of (x+b)

    @precondition: every entry in y_idx is a valid (non-negative) column index into x

    This L{Op} has three outputs:
     - KL(softmax(x+b), y)
     - softmax(x+b)
     - argmax(x+b)

    softmax(x[i]) is the i'th distribution over len(x[i]) options
    argmax(x) is the index of x's greatest element
    y_idx[i] is an integer index, encoding a 1-hot distribution. 

    In practice, when we are trying to do classification, we have one row in x
    and y_idx per example, and y[i] is the index of the (correct) class of the
    i'th example.

    """
    nin=3
    nout=3
    def __init__(self, **kwargs):
        gof.Op.__init__(self, **kwargs)
    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return tensor.hashtype(self)

    def make_node(self, x, b, y_idx):
        x = tensor.as_tensor_variable(x)
        b = tensor.as_tensor_variable(b)
        y_idx = tensor.as_tensor_variable(y_idx)
        if x.type.ndim != 2 \
                or x.type.dtype not in ['float32', 'float64']:
            raise ValueError('x must be 2-d tensor of floats')
        if b.type.ndim != 1 \
                or x.type.dtype not in ['float32', 'float64']:
            raise ValueError('b must be 1-d tensor of floats')
        if y_idx.type.ndim != 1 \
                or y_idx.type.dtype not in ['int8', 'int16', 'int32', 'int64']:
            raise ValueError('y_idx must be 1-d tensor of ints')

#       TODO: Is this correct? It used to be y, not y_idx
        nll = tensor.TensorType(x.type.dtype,
                y_idx.type.broadcastable).make_variable()
#        nll = TensorType(x.dtype, y.broadcastable)
        sm = x.type.make_variable()
        am = y_idx.type.make_variable()
        return gof.Apply(self, [x, b, y_idx], [nll, sm, am])
    def perform(self, node, input_storage, output_storage):
        """
        The math, where x is an input vector, and t is a target index:

            softmax(x)[i] = exp(x[i]) / sum_j(exp(x[j]))
            nll(x,t) = -log(softmax(x)[t])

        We compute this by subtracting off the max of x. This avoids numerical instability.

            m = max_j x[j]
            softmax(x)[i] = exp(x[i] -m) / sum_j(exp(x[j] - m))

            nll = -log(exp(x[t] -m) / sum_j(exp(x[j] - m)))
                = -x[t] + m + log( sum_j(exp(x[j] - m)))

        """
        x, b, y_idx = input_storage
        if b.shape[0] != x.shape[1]:
            raise ValueError('b must have same number of columns as x')
        if y_idx.shape[0] != x.shape[0]:
            raise ValueError('y_idx must have same number of rows as x')

        sm = numpy.zeros_like(x) # softmax
        nll = numpy.zeros(x.shape[0], dtype=node.outputs[0].type.dtype) #nll(y | softmax(x))
        am = numpy.zeros_like(y_idx)
        for i in xrange(sm.shape[0]):
            #add the bias vector to the i'th row of x
            row = x[i] + b 

            #get the maximum value of i'th row for numerically safe softmax / nll
            am[i] = numpy.argmax(row)
            m = row[am[i]]

            #compute the unnormalized softmax, and normalization constant
            sm[i] = numpy.exp(row - m) 
            sum_j = numpy.sum(sm[i]) # sum_j(exp(x[j] - m))

            #normalized our softmax
            sm[i] *= 1.0 / sum_j

            # store the nll
            nll[i] = -row[y_idx[i]] + m + numpy.log(sum_j)
            
        output_storage[0][0] = nll
        output_storage[1][0] = sm
        output_storage[2][0] = am
    def grad(self, (x, b, y_idx), (g_nll, g_sm, g_am)):
        if g_sm is not None or g_am is not None:
            raise NotImplementedError()
        nll, sm = crossentropy_softmax_1hot_with_bias(x, b, y_idx)
        #dx = CrossentropySoftmax1HotWithBiasDx()(g_nll, sm, y_idx)
        dx = crossentropy_softmax_1hot_with_bias_dx(g_nll, sm, y_idx)
        db = tensor.sum(dx, axis = [0])
        return dx, db, None

    def c_headers(self):
        return ['<iostream>', '<cmath>']

    @staticmethod
    def c_code_template():
        # this implementation was lifted from
        # /u/bergstrj/cvs/bergstrj/src/feb07/nn.cxx

        #TODO: put this into a templated function, in the support code
        #TODO: declare the max of each row as an Op output

        #TODO: set error messages for failures in this code

        #TODO: use this to accept float32 and int32: node.inputs[0].type.dtype_specs()[1]
        (init_decl, begin_row_loop, inside_row_loop, end_row_loop) = \
                SoftmaxWithBias.c_code_template()
        return (init_decl,
                """
        if (%(y_idx)s->nd != 1)
        {
            PyErr_SetString(PyExc_ValueError, "y_idx not 1d tensor");
            %(fail)s;
        }
        if ((%(y_idx)s->descr->type_num != PyArray_INT64)
            && (%(y_idx)s->descr->type_num != PyArray_INT32)
            && (%(y_idx)s->descr->type_num != PyArray_INT16)
            && (%(y_idx)s->descr->type_num != PyArray_INT8))
        {
            PyErr_SetString(PyExc_TypeError, "y_idx not int8, int16, int32, or int64");
            %(fail)s;
        }
        if (%(x)s->dimensions[0] != %(y_idx)s->dimensions[0])
        {
            PyErr_Format(PyExc_ValueError, "number of rows in x (%%zi) does not match length of y (%%zi)",
            %(x)s->dimensions[0], %(y_idx)s->dimensions[0]);
            %(fail)s;
        }

        if ((NULL == %(nll)s) //initial condition
            || (%(nll)s->dimensions[0] != %(y_idx)s->dimensions[0]))
        {
            if (NULL != %(nll)s) Py_XDECREF(%(nll)s);
            %(nll)s = (PyArrayObject*)PyArray_SimpleNew(1, PyArray_DIMS(%(y_idx)s), type_num_%(x)s);
            if(!%(nll)s)
            {
                PyErr_SetString(PyExc_MemoryError, "failed to alloc nll output");
                %(fail)s;
            }
        }
        if ((NULL == %(am)s)
            || (%(am)s->dimensions[0] != %(y_idx)s->dimensions[0]))
        {
            Py_XDECREF(%(am)s);
            %(am)s = (PyArrayObject*) PyArray_SimpleNew(1, PyArray_DIMS(%(y_idx)s), type_num_%(y_idx)s);
            if(!%(am)s)
            {
                PyErr_SetString(PyExc_MemoryError, "failed to alloc am output");
                %(fail)s;
            }
        }
                """,
                begin_row_loop,
                """
            const %(y_idx_type)s y_i = ((%(y_idx_type)s*)(%(y_idx)s->data + %(y_idx)s->strides[0] * i))[0];
            dtype_%(nll)s* __restrict__ nll_i = (dtype_%(nll)s*)(%(nll)s->data + %(nll)s->strides[0] * i);
            %(am_type)s* __restrict__ am_i = (%(am_type)s*) (%(am)s->data + %(am)s->strides[0] * i);
                """,
                inside_row_loop,
                """
            if ((y_i >= %(x)s->dimensions[1]) || (y_i < 0))
            {
                PyErr_SetString(PyExc_ValueError, "y_i value out of bounds");
                %(fail)s;
            }
            nll_i[0] = - x_i[y_i*Sx]
                       - b_i[y_i*Sb]
                       + row_max
                       + log(sum);
            am_i[0] = row_max_j;
                """,
                end_row_loop)


    def c_code_cache_version(self):
        return (5,) + SoftmaxWithBias.c_code_cache_version()
    def c_code(self, node, name, (x, b, y_idx), (nll, sm, am), sub):
        y_idx_type = node.inputs[2].type.dtype_specs()[1]
        am_type = y_idx_type
        code_template = ''.join(self.c_code_template())
        return code_template % dict(locals(), **sub)

class CrossentropySoftmax1HotWithBiasDx (gof.Op):
    nin=3
    nout=1
    """Gradient wrt x of the CrossentropySoftmax1Hot Op"""
    def __init__(self, **kwargs):
        gof.Op.__init__(self,**kwargs)
    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return tensor.hashtype(self)
    def make_node(self, dy, sm, y_idx,**kwargs):
        dy = tensor.as_tensor_variable(dy)
        sm = tensor.as_tensor_variable(sm)
        y_idx = tensor.as_tensor_variable(y_idx)
        return gof.Apply(self, [dy, sm, y_idx],[sm.type.make_variable()])
    def perform(self, node, input_storage, output_storage):
        dy,sm,y_idx = input_storage
        dx = numpy.zeros_like(sm)
        for i in xrange(sm.shape[0]):
            dx[i] = dy[i] * sm[i] #vector scale
            dx[i, y_idx[i]] -= dy[i] #scalar decrement
        output_storage[0][0] = dx
    def grad(self, (dy, sm, y_idx), (g_dx, )):
        # Note: currently we do not care about computing the gradient of dy,
        # since we usually should not need it.
        return [None, dy * g_dx, None]
    def c_code_cache_version(self):
        return (2,)
    def c_code(self, node, name, (dnll, sm, y_idx), (dx,), sub):
        y_idx_type = node.inputs[2].type.dtype_specs()[1]
        return """

        if ((%(dnll)s->descr->type_num != PyArray_DOUBLE) && (%(dnll)s->descr->type_num != PyArray_FLOAT))
        {
            PyErr_SetString(PyExc_TypeError, "dnll type should be float32 or float64");
            %(fail)s;
        }
        if ((%(sm)s->descr->type_num != PyArray_DOUBLE) && (%(sm)s->descr->type_num != PyArray_FLOAT))
        {
            PyErr_SetString(PyExc_TypeError, "sm type should be float32 or float64");
            %(fail)s;
        }
        if ((%(y_idx)s->descr->type_num != PyArray_INT64)
            && (%(y_idx)s->descr->type_num != PyArray_INT32)
            && (%(y_idx)s->descr->type_num != PyArray_INT16)
            && (%(y_idx)s->descr->type_num != PyArray_INT8))
        {
            PyErr_SetString(PyExc_TypeError, "y_idx not int8, int16, int32, or int64");
            %(fail)s;
        }
        if ((%(dnll)s->nd != 1)
            || (%(sm)s->nd != 2)
            || (%(y_idx)s->nd != 1))
        {
            PyErr_SetString(PyExc_ValueError, "rank error");
            %(fail)s;
        }
        if (%(dnll)s->dimensions[0] != %(sm)s->dimensions[0])
        {
            PyErr_Format(PyExc_ValueError, "dnll.shape[0] (%%d) != sm.shape[0] (%%d)",
                        %(dnll)s->dimensions[0], %(sm)s->dimensions[0]);
            //PyErr_SetString(PyExc_ValueError, "dnll.shape[0] != sm.shape[0]");
            %(fail)s;
        }
        if (%(dnll)s->dimensions[0] != %(y_idx)s->dimensions[0])
        {
            PyErr_SetString(PyExc_ValueError, "dnll.shape[0] != y_idx.shape[0]");
            %(fail)s;
        }
        if ((NULL == %(dx)s)
            || (%(dx)s->dimensions[0] != %(sm)s->dimensions[0])
            || (%(dx)s->dimensions[1] != %(sm)s->dimensions[1]))
        {
            if (NULL != %(dx)s) Py_XDECREF(%(dx)s);
            %(dx)s = (PyArrayObject*)PyArray_SimpleNew(2, PyArray_DIMS(%(sm)s), type_num_%(sm)s);
            if(!%(dx)s) {
                PyErr_SetString(PyExc_MemoryError, "failed to alloc dx output");
                %(fail)s
            }
        }

        for (size_t i = 0; i < %(dx)s->dimensions[0]; ++i)
        {
            const dtype_%(dnll)s dnll_i = ((dtype_%(dnll)s*)(%(dnll)s->data + %(dnll)s->strides[0] * i))[0];

            const %(y_idx_type)s y_i = ((%(y_idx_type)s*)(%(y_idx)s->data + %(y_idx)s->strides[0] * i))[0];

            const dtype_%(sm)s* __restrict__ sm_i = (dtype_%(sm)s*)(%(sm)s->data + %(sm)s->strides[0] * i);
            npy_intp Ssm = %(sm)s->strides[1]/sizeof(dtype_%(sm)s);

            dtype_%(dx)s* __restrict__ dx_i = (dtype_%(dx)s*)(%(dx)s->data + %(dx)s->strides[0] * i);
            npy_intp Sdx = %(dx)s->strides[1]/sizeof(dtype_%(dx)s);

            for (size_t j = 0; j < %(dx)s->dimensions[1]; ++j)
            {
                dx_i[j * Sdx] = dnll_i * sm_i[j * Ssm];
            }
            if (y_i >= %(dx)s->dimensions[1])
            {
                PyErr_SetString(PyExc_ValueError, "y_i >= dx dimensions[1]");
                %(fail)s;
            }
            dx_i[y_i * Sdx] -= dnll_i;
        }
        """ % dict(locals(), **sub)

crossentropy_softmax_argmax_1hot_with_bias = \
    CrossentropySoftmaxArgmax1HotWithBias()

crossentropy_softmax_1hot_with_bias_dx = \
    CrossentropySoftmax1HotWithBiasDx()

def crossentropy_softmax_1hot_with_bias(x, b, y_idx, **kwargs):
    return crossentropy_softmax_argmax_1hot_with_bias(x, b, y_idx, **kwargs)[0:2]

def crossentropy_softmax_1hot(x, y_idx, **kwargs):
    b = tensor.zeros_like(x[0,:])
    return crossentropy_softmax_1hot_with_bias(x, b, y_idx, **kwargs)

def crossentropy_softmax_max_and_argmax_1hot_with_bias(x, b, y_idx, **kwargs):
    """
    @return: The cross-entropy, the softmax output, the max probability, and the argmax index
    @todo: Since we are recomputing the argmax, we might as well assert that it is correct.
    @todo: Make this entire function is
    unnecessary? e.g. CrossentropySoftmaxArgmax1HotWithBias should return
    the appropriate information (i.e. the max probability)?
    """
    (xent, softmax) = crossentropy_softmax_1hot_with_bias(x, b, y_idx, **kwargs)
    (max_pr, argmax) = tensor.max_and_argmax(softmax)
    return (xent, softmax, max_pr, argmax)
def crossentropy_softmax_max_and_argmax_1hot(x, y_idx, **kwargs):
    b = tensor.zeros_like(x[0,:])
    return crossentropy_softmax_max_and_argmax_1hot_with_bias(x, b, y_idx, **kwargs)

class CrossentropyCategorical1HotGrad(gof.Op):

    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return tensor.hashtype(self)
    def make_node(self, g_y, coding_dist, true_one_of_n):
        return gof.Apply(self, [g_y, coding_dist, true_one_of_n], [coding_dist.type()])
    def perform(self, node, (g_y, coding_dist, true_one_of_n), (g_coding_strg,)):
        g_coding = numpy.zeros_like(coding_dist)
        for i in xrange(len(g_y)):
            g_coding[i, true_one_of_n[i]] = -g_y[i]/coding_dist[i, true_one_of_n[i]]

        g_coding_strg[0] = g_coding
crossentropy_categorical_1hot_grad = CrossentropyCategorical1HotGrad()

class CrossentropyCategorical1Hot(gof.Op):

    """Compute the cross entropy between a coding distribution and 
    a true distribution of the form [0, 0, ... 0, 1, 0, ..., 0]

    .. math::

        y[i] = - \log(coding_dist[i, one_of_n[i])


    :note:
    In the case that the coding distribution is the output of a softmax, an application of this
    Op will probably be optimized away in favour of one with a C implementation.

    """

    def __eq__(self, other):
        return type(self) == type(other)
    def __hash__(self):
        return tensor.hashtype(self)
    def make_node(self, coding_dist, true_one_of_n):
        """
        :type coding_dist: dense matrix

        :type true_one_of_n: lvector

        :rtype: dvector
        """
        _coding_dist = tensor.as_tensor_variable(coding_dist)
        _true_one_of_n = tensor.as_tensor_variable(true_one_of_n)
        if _coding_dist.type.ndim != 2:
            raise TypeError('matrix required for argument: coding_dist')
        if _true_one_of_n.type not in (tensor.lvector, tensor.ivector):
            raise TypeError('integer vector required for argument: true_one_of_n'
                    '(got type: %s instead of: %s)' % (_true_one_of_n.type,
                        tensor.lvector))

        return gof.Apply(self, [_coding_dist, _true_one_of_n],
                [tensor.Tensor(dtype=_coding_dist.dtype, broadcastable=[False])()])

    def perform(self, node, (coding, one_of_n), (y_out,)):
        y = numpy.zeros_like(coding[:,0])
        for i in xrange(len(y)):
            y[i] = -numpy.log(coding[i, one_of_n[i]])
        y_out[0] = y
    
    def grad(self, (coding, one_of_n), (g_y,)):
        return [crossentropy_categorical_1hot_grad(g_y, coding, one_of_n), None]
crossentropy_categorical_1hot = CrossentropyCategorical1Hot()

@gof.optimizer
def crossentropy_to_crossentropy_with_softmax(env):
    #not a local optimization because we are replacing outputs from several nodes at once

    def search_make_one_sub():
        for node in env.toposort():
            if node.op == crossentropy_categorical_1hot:
                nll, = node.outputs
                sm, one_of_n = node.inputs
                if sm.owner and sm.owner.op == softmax:
                    x, = sm.owner.inputs
                    new_nll, new_sm, new_am = crossentropy_softmax_argmax_1hot_with_bias(x,
                            tensor.zeros_like(x[0]), one_of_n)
                    env.replace_all_validate([(nll, new_nll),(sm, new_sm)],
                            reason="crossentropy_to_crossentropy_with_softmax")
                    return True
                if sm.owner and sm.owner.op == softmax_with_bias:
                    x, b = sm.owner.inputs
                    new_nll, new_sm, new_am = crossentropy_softmax_argmax_1hot_with_bias(x, b,
                            one_of_n)
                    env.replace_all_validate([(nll, new_nll),(sm, new_sm)],
                            reason="crossentropy_to_crossentropy_with_softmax")
                    return True

        return False

    while search_make_one_sub():
        pass
    return
optdb.register('XentThing', crossentropy_to_crossentropy_with_softmax, 60.00,
        'fast_run', 'inplace', 'xent')

@gof.local_optimizer([softmax_grad])
def local_crossentropy_to_crossentropy_with_softmax_grad(node):
    if node.op == softmax_grad:
        g_coding_dist, coding_dist = node.inputs
        if g_coding_dist.owner and g_coding_dist.owner.op == crossentropy_categorical_1hot_grad:
            g_nll, coding_dist, true_one_of_n = g_coding_dist.owner.inputs
            dx = crossentropy_softmax_1hot_with_bias_dx(g_nll, coding_dist, true_one_of_n)
            return [dx]
opt.register_specialize(local_crossentropy_to_crossentropy_with_softmax_grad)

@opt.register_specialize
@gof.local_optimizer([tensor._max_and_argmax])
def local_argmax_pushdown(node):
    if node.op == tensor._max_and_argmax:
        x_max, x_argmax = node.outputs
        x, axis = node.inputs
        #TODO: Make a list/set of monotonic ops...
        if x.owner and x.owner.op in (softmax, softplus, tensor.exp, tensor.log, tensor.tanh,
                sigmoid):
            pre_x, = x.owner.inputs
            return tensor._max_and_argmax(pre_x, axis)
        if x.owner and x.owner.op == softmax_with_bias:
            pre_x, pre_bias = x.owner.inputs
            return tensor._max_and_argmax(pre_x+tensor.DimShuffle(pre_bias.broadcastable,
                ('x',0))(pre_bias), axis)

# Utility function used by the two next optimizations
def _check_rows_is_arange_len_labels(rows, labels):
    '''Check that 'rows' is the same node as T.arange(labels.shape[0])'''

    # this is admittedly a pretty random thing to have here... but it's not wrong (I think)
    # and it has the effect of making the advanced_indexing -> crossentropy optimization work
    # in the case where the labels are float32s casted to integers.  "Why would anyone do that?"
    # you ask... it is a handy trick for storing labels on a pre-FERMI GPU device so that
    # logistic regression goes faster.
    if labels.owner and labels.owner.op == tensor._convert_to_int32:
        labels = labels.owner.inputs[0]

    if rows.owner and isinstance(rows.owner.op, tensor.ARange):
        start, stop, step = rows.owner.inputs
        if getattr(start, 'data', None) != 0: #constants will have data
            return False
        if getattr(step, 'data', None) != 1: # constant step will have data
            return False
        if stop.owner and isinstance(stop.owner.op, tensor.Subtensor):
            shape_subtensor = stop.owner
            if shape_subtensor.op.idx_list == [0]:
                shape_var, = shape_subtensor.inputs
                if shape_var.owner and shape_var.owner.op == tensor._shape:
                    return shape_var.owner.inputs[0] is labels

@gof.local_optimizer([tensor._shape])
def local_shape_lift_advanced_indexing_arange(node):
    '''shape(a[arange(len(y)), y]) -> shape(y) (conditions apply)'''
    if node.op == tensor._shape:
        if node.inputs[0].owner and \
                isinstance(node.inputs[0].owner.op, tensor.AdvancedSubtensor):
            try:
                a, rows, labels = node.inputs[0].owner.inputs
            except:
                return
            if _check_rows_is_arange_len_labels(rows, labels):
                if labels.ndim == 1 and a.ndim == 2:
                    return tensor._shape(labels),
opt.register_specialize(local_shape_lift_advanced_indexing_arange, 'shape_lift')


@opt.register_specialize
@gof.local_optimizer([])
def local_advanced_indexing_crossentropy_onehot(node):
    log = None
    sm = None
    # First case: log(softmax(x))[rows, labels]
    if isinstance(node.op, tensor.AdvancedSubtensor):
        try:
            log, rows, labels = node.inputs
        except:
            pass
        if log and log.owner and log.owner.op == tensor.log:
            sm = log.owner.inputs[0]

    # Second case: log(softmax(x)[rows, labels])
    if node.op == tensor.log:
        pre_log = node.inputs[0].owner
        if pre_log and isinstance(pre_log.op, tensor.AdvancedSubtensor):
            try:
                sm, rows, labels = pre_log.inputs
            except:
                pass


    if sm is not None and sm.owner and sm.owner.op in (softmax, softmax_with_bias):
        sm_w_bias = local_softmax_with_bias.transform(sm.owner)
        if sm_w_bias:
            assert sm_w_bias[0].owner.op == softmax_with_bias
            x_var, b_var = sm_w_bias[0].owner.inputs
        else:
            x_var = sm.owner.inputs[0]
            b_var = tensor.zeros_like(x_var[0])

        # Check that rows == arange(labels.shape[0])
        if _check_rows_is_arange_len_labels(rows, labels):
            if labels.ndim == 1 and x_var.ndim == 2:
                return [-crossentropy_softmax_argmax_1hot_with_bias(x_var, b_var, labels)[0]]

@opt.register_specialize
@gof.local_optimizer([softmax_grad])
def local_advanced_indexing_crossentropy_onehot_grad(node):
    if not (node.op == softmax_grad):
        return

    sm = None
    try:
        out_grad, sm = node.inputs
    except:
        return

    if sm is not None and sm.owner and sm.owner.op in (softmax, softmax_with_bias):
        sm_w_bias = local_softmax_with_bias.transform(sm.owner)
        if sm_w_bias:
            assert sm_w_bias[0].owner.op == softmax_with_bias
            x_var, b_var = sm_w_bias[0].owner.inputs
        else:
            x_var = sm.owner.inputs[0]
    else:
        return

    # Two base cases are supported:
    # 1. AdvancedIncSubtensor(
    #           zeros_like(softmax(x)),
    #           -1. / AdvancedSubtensor(softmax(x), arange(y.shape[0]), y),
    #           arange(y.shape[0]),
    #           y)
    #   which arises from the gradient of log(softmax(x)[arange(y.shape[0]), y])
    #
    # 2. AdvancedIncSubtensor(
    #           zeros_like(log(softmax(x))),
    #           -1. like (AdvancedSubtensor(log(softmax(x)), arange(y.shape[0]), y)),
    #           arange(y.shape[0]),
    #           y)
    #           / softmax(x)
    #   which arises from the gradient of log(softmax(x))[arange(y.shape[0]), y]
    #
    # In some cases, in case 2., insted of "-1. like (AdvancedSubtensor...)",
    # we can have "-1. like ([-1] * AdvancedSubtensor...)". This case will be
    # recognized too, but other variants, even with the same shape, might not
    # (yet).

    # The base cases are realized when the gradient of the
    # cost wrt the output is equal to 1.  When this gradient
    # has another (scalar) value, it typically appears in the
    # second argument of AdvancedIncSubtensor. In that case, we
    # try to extract it, and feed it as the output gradient of
    # crossentropy_softmax_1hot_with_bias_dx.

    #
    # N.B. Regarding clients -- This substitution is important for numerical stability, so we
    # perform the substitution even when intermediate values have multiple clients.
    #

    # First case.
    # After the check for AdvancedIncSubtensor, if anything does not fit with
    # the formula above, there's no way to fit it with the the second case,
    # so we return immediately.
    if out_grad.owner and isinstance(out_grad.owner.op, tensor.AdvancedIncSubtensor):
        try:
            z, incr, rows, labels = out_grad.owner.inputs
        except:
            return

        # Check that z == zeros_like(softmax(x))
        if z.owner and z.owner.op == tensor.fill:
            model, value = z.owner.inputs

            if not (model is sm and hasattr(value, 'data') and numpy.all(value.data == 0)):
                return
            #else: OK
        else:
            return

        # In the base case (output gradient = 1), incr is -1./sm[arange(len(y)), y]
        # Here, we are looking for the AdvancedSubtensor term (sm[arange(len(y)), y]),
        # the remaining of the expression will be used to compute outgrad_factor
        # outgrad_factor will be constructed in 3 steps as follow:
        # outgrad_factor = +/- 1 (initial sign)
        # outgrad_factor *= numerator
        # outgrad_factor /= denominator
        adv_subtensor = None
        outgrad_factor = 1.

        # If there's a 'minus' sign before the whole expression, put it in
        # outgrad_factor and iterate
        if incr.owner and incr.owner.op == tensor.neg:
            outgrad_factor = -1.
            incr = incr.owner.inputs[0]

        if incr.owner and incr.owner.op == tensor.true_div:
            num, denom = incr.owner.inputs

            # set outgrad_factor according to the numerator,
            # it may be divided later
            if hasattr(num, 'data') and numpy.all(num.data == -1):
                # Base case, num is -1
                outgrad_factor *= 1.
            elif numpy.all(num.broadcastable):
                # Otherwise, it should be a scalar
                outgrad_factor *= -num
            else:
                return

            if not denom.owner:
                return

            if isinstance(denom.owner.op, tensor.AdvancedSubtensor):
                # Base case
                adv_subtensor = denom
                outgrad_factor /= 1.
            elif denom.owner.op == tensor.mul:
                # Try to find the AdvancedSubtensor node mentionned above,
                # and a scalar that is equal to the output gradient
                for i, input in enumerate(denom.owner.inputs):
                    if input.owner and isinstance(input.owner.op, tensor.AdvancedSubtensor):
                        other_inputs = [in_ for (j, in_) in enumerate(denom.owner.inputs) if j!=i]
                        if len(other_inputs) == 1:
                            rest = other_inputs[0]
                        else:
                            rest = tensor.mul(*[other_inputs])

                        # Check that rest is a scalar
                        if numpy.all(rest.broadcastable):
                            adv_subtensor = input
                            outgrad_factor /= rest
                            break

            else:
                return

            if adv_subtensor is not None:
                try:
                    maybe_sm, maybe_rows, maybe_labels = adv_subtensor.owner.inputs
                except:
                    return

                if not (maybe_sm is sm and maybe_rows is rows and maybe_labels is labels):
                    return
                #else: OK
            else:
                return
        else:
            return

        # Check that rows is arange(labels.shape[0])
        if not _check_rows_is_arange_len_labels(rows, labels):
            return

        # else, arguments of AdvancedIncSubtensor are OK,
        # it was really case 1.

    # Second case
    elif out_grad.owner and out_grad.owner.op == tensor.true_div:
        try:
            num, denom = out_grad.owner.inputs
        except:
            return

        # Check the numerator (AdvancedIncSubtensor)
        if num.owner and isinstance(num.owner.op, tensor.AdvancedIncSubtensor):
            try:
                z, incr, rows, labels = num.owner.inputs
            except:
                return

            # Check z is zeros_like(log(sm))
            if z.owner and z.owner.op == tensor.fill:
                model, value = z.owner.inputs

                if model.owner and model.owner.op == tensor.log:
                    if sm is model.owner.inputs[0]:
                        log_sm = model
                    else:
                        return

                    if not (hasattr(value, 'data') and numpy.all(value.data == 0)):
                        return
                    #else: OK
                else:
                    return
            else:
                return

            # Check incr is ((-1.) like log(softmax(x))[arange(len(y)), y])
            if incr.owner and incr.owner.op == tensor.fill:
                model, value = incr.owner.inputs
                adv_subtensor = None
                outgrad_factor = None
                if model.owner and isinstance(model.owner.op, tensor.AdvancedSubtensor):
                    adv_subtensor = model
                else:
                    if model.owner and isinstance(model.owner.op, tensor.Elemwise):
                        for input in model.owner.inputs:
                            if input.owner and isinstance(input.owner.op, tensor.AdvancedSubtensor):
                                adv_subtensor = input
                                break
                                #TODO: try them all, not just the first one
                    else:
                        return

                if adv_subtensor is not None:
                    try:
                        maybe_log_sm, maybe_rows, maybe_labels = adv_subtensor.owner.inputs
                    except:
                        return

                    if not (maybe_log_sm is log_sm and maybe_rows is rows and maybe_labels is labels):
                        return
                    #else: OK
                else:
                    return

                # In the base case, value is the constant '-1'
                if hasattr(value, 'data') and numpy.all(value.data == -1):
                    outgrad_factor = 1.
                # Otherwise, it should be a scalar, and the output gradient
                # would be -value
                elif numpy.all(value.broadcastable):
                    outgrad_factor = -value
                else:
                    return

            else:
                return

            # Check that rows is arange(labels.shape[0])
            if not _check_rows_is_arange_len_labels(rows, labels):
                return

            # else, arguments of AdvancedIncSubtensor are OK

        # Check the denominator (sm)
        if not denom is sm:
            return

        # else, numerator and denominator are OK,
        # it was really case 2.

    else:
        return

    # Dimension check before substitution
    if labels.ndim == 1 and x_var.ndim == 2:
        if outgrad_factor is not None:
            out_grad = tensor.fill(x_var[:,0], outgrad_factor)
            return [crossentropy_softmax_1hot_with_bias_dx(out_grad, sm, labels)]
        else:
            return
    else:
        return

@opt.register_specialize
@gof.local_optimizer([softmax_with_bias])
def graph_merge_softmax_with_crossentropy_softmax(node):
    if node.op == softmax_with_bias:
        x, b = node.inputs
        for x_client in x.clients:
            if x_client[0].op == crossentropy_softmax_argmax_1hot_with_bias:
                big_client = x_client[0]
                if big_client in [b_client[0] for b_client in b.clients]:
                    xx, bb, ll = big_client.inputs
                    mergeable_client = big_client.op(x, b, ll)
                    return [mergeable_client[1]]


def binary_crossentropy(output, target):
    """
    Compute the crossentropy of binary output wrt binary target.
    @note: We do not sum, crossentropy is computed by component.
    @todo: Rewrite as a scalar, and then broadcast to tensor.
    @todo: This is essentially duplicated as cost.cross_entropy
    @warning: OUTPUT and TARGET are reversed in cost.cross_entropy
    """
    return -(target * tensor.log(output) + (1.0 - target) * tensor.log(1.0 - output))

def categorical_crossentropy(coding_dist, true_dist):
    """
    WARNING: THIS FUNCTION IS UNNECESSARILY POLYMORPHIC.
    We ultimately don't want the polymorphism, and will move this function to pylearn.algorithms.cost.
    The 1hot version will be removed.
    The length of the documentation here is a form of code smell.
    
    Return the cross-entropy between an approximating distribution and a true distribution

    The cross entropy between two probability distributions measures the average number of bits
    needed to identify an event from a set of possibilities, if a coding scheme is used based
    on a given probability distribution q, rather than the "true" distribution p.

    Mathematically it is defined as follows:

    .. math::

        H(p,q) = - \sum_x p(x) \log(q(x))

    :type coding_dist: a dense matrix.
    :param coding_dist: Each slice along axis represents one distribution.

    :type true_dist: a dense matrix or sparse matrix or integer vector.
    :param coding_dist: In the case of a matrix argument, each slice along axis represents one
    distribution.  In the case of an integer vector argument, each element represents the
    position of the '1' in a 1-of-N encoding.

    :type axis: int
    :param axis: the dimension over which each distribution runs. (1 for row distributions, 0
    for column distributions)

    :rtype: tensor of rank one-less-than `coding_dist`
    :returns: the cross entropy between each coding and true distribution.

    """
    if true_dist.ndim == coding_dist.ndim:
        return -theano.sum(true_dist * log(coding_dist), axis=coding_dist.ndim-1)
    elif true_dist.ndim == coding_dist.ndim - 1:
        return crossentropy_categorical_1hot(coding_dist, true_dist)
    else:
        raise TypeError('rank mismatch between coding and true distributions')



class Prepend_scalar_constant_to_each_row(gof.Op):
    def __init__(self, val = 0):
        if isinstance(val, float):
            val = scalar.constant(val)
        self.val = val

    def __eq__(self, other):
        return (type(self) == type(other)) and (self.val == other.val)
    def __hash__(self):
        return tensor.hashtype(self) ^ hash(self.val.value)

    def make_node(self, mat):
        #check type of input
        if not isinstance(mat,gof.Variable) or not mat.type==tensor.matrix().type:
            raise TypeError("Expected a matrix as input")
        x = tensor.as_tensor_variable(mat)
        y = tensor.as_tensor_variable(self.val)
        if x.type.dtype != y.type.dtype:
            TypeError("the value to prepend don't have the same type as the matrix")

        node = gof.Apply(op=self, inputs=[mat], outputs=[tensor.matrix()])
        return node

    def perform(self, node, (mat, ), (output, )):
        new_shape=(mat.shape[0],mat.shape[1]+1)
        if output[0] == None:
            output[0]=numpy.empty(new_shape,dtype=mat.dtype)
            out=output[0]
        else:
            if output[0].shape!=new_shape:
                try:
                    output[0].resize(new_shape)
                except:
                    output[0]=numpy.empty(new_shape, dtype=mat.dtype)
            out=output[0]

        out[:,0].fill(self.val.data)
        out[:,1:]=mat

    def grad(self, (mat,), (goutput,)):
        return goutput[:,1:]

class Prepend_scalar_to_each_row(gof.Op):
    def __eq__(self, other):
        return (type(self) == type(other))
    def __hash__(self):
        return tensor.hashtype(self)

    def make_node(self, val, mat):
        #check type of input
        if isinstance(val, float):
            val = scalar.constant(val)
        if not isinstance(mat,gof.Variable) or not mat.type==tensor.matrix().type:
            raise TypeError("Expected a matrix as input")
        x = tensor.as_tensor_variable(mat)
        y = tensor.as_tensor_variable(val)
        if x.type.dtype != y.type.dtype:
            TypeError("the value to prepend don't have the same type as the matrix")

        node = gof.Apply(op=self, inputs=[val,mat], outputs=[tensor.matrix()])
        return node

    def perform(self, node, (val,mat), (output, )):
        new_shape=(mat.shape[0],mat.shape[1]+1)
        if output[0] == None:
            output[0]=numpy.empty(new_shape,dtype=mat.dtype)
            out=output[0]
        else:
            if output[0].shape!=new_shape:
                try:
                    output[0].resize(new_shape)
                except:
                    output[0]=numpy.empty(new_shape, dtype=mat.dtype)
            out=output[0]
        out[:,0].fill(val)
        out[:,1:]=mat

    def grad(self, (val, mat), (goutput,)):
        return goutput[:,0], goutput[:,1:]

prepend_scalar_to_each_row = Prepend_scalar_to_each_row()
prepend_0_to_each_row = Prepend_scalar_constant_to_each_row(0.)
prepend_1_to_each_row = Prepend_scalar_constant_to_each_row(1.)

logsigm_to_softplus = gof.PatternSub(
    (tensor.log, (sigmoid, 'x')),
    (tensor.neg, (softplus, (tensor.neg, 'x'))),
    allow_multiple_clients = True)
log1msigm_to_softplus = gof.PatternSub(
    (tensor.log, (tensor.sub, tensor.constant([[1.0]]), (sigmoid, 'x'))),
    (tensor.neg, (softplus, 'x')),
    allow_multiple_clients = True)

opt.register_specialize(logsigm_to_softplus, name = 'logsigm_to_softplus')
opt.register_specialize(log1msigm_to_softplus, name = 'log1msigm_to_softplus')
