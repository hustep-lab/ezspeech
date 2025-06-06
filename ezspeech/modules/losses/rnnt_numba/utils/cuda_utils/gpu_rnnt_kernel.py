# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright 2018-2019, Mingkun Huang
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math

import torch
from numba import cuda

from ezspeech.modules.losses.rnnt_numba.utils import rnnt_helper

GPU_RNNT_THREAD_SIZE = 256

INF = 10000.0


@cuda.jit(device=True, inline=True)
def logp(
    denom: torch.Tensor,
    acts: torch.Tensor,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    mb: int,
    t: int,
    u: int,
    v: int,
):
    """
    Compute the sum of log probability from the activation tensor and its denominator.

    Args:
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        acts: Tensor of shape [B, T, U, V+1] flattened. Represents the logprobs activation tensor.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        mb: Batch indexer.
        t: Acoustic sequence timestep indexer.
        u: Target sequence timestep indexer.
        v: Vocabulary token indexer.

    Returns:
        The sum of logprobs[mb, t, u, v] + denom[mb, t, u]
    """
    col = (mb * maxT + t) * maxU + u
    return denom[col] + acts[col * alphabet_size + v]


@cuda.jit(device=True, inline=True)
def logp_duration(
    acts: torch.Tensor,
    maxT: int,
    maxU: int,
    num_durations: int,
    mb: int,
    t: int,
    u: int,
    v: int,
):
    col = (mb * maxT + t) * maxU + u
    return acts[col * num_durations + v]


@cuda.jit()
def compute_alphas_kernel(
    acts: torch.Tensor,
    denom: torch.Tensor,
    alphas: torch.Tensor,
    llForward: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
):
    """
    Compute alpha (forward variable) probabilities over the transduction step.

    Args:
        acts: Tensor of shape [B, T, U, V+1] flattened. Represents the logprobs activation tensor.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        alphas: Zero tensor of shape [B, T, U]. Will be updated inside the kernel with the forward variable
            probabilities.
        llForward: Zero tensor of shape [B]. Represents the log-likelihood of the forward pass.
            Returned as the forward pass loss that is reduced by the optimizer.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT blank token in the vocabulary. Generally the first or last token in the vocab.

    Updates:
        Kernel inplace updates the following inputs:
        -   alphas: forward variable scores.
        -   llForward: log-likelihood of forward variable.
    """
    # // launch B blocks, each block has U threads
    b = cuda.blockIdx.x  # // batch id
    u = cuda.threadIdx.x  # label id, u
    T = xlen[b]  # select AM length of current sample
    U = ylen[b] + 1  # select target length of current sample, +1 for the blank token

    labels: torch.Tensor = mlabels[
        b
    ]  # mb label start point, equivalent to mlabels + b * (maxU - 1)
    offset = b * maxT * maxU  # pointer indexing offset

    # alphas += offset # pointer offset, ignored since we explicitly add offset

    # Initilize alpha[b, t=0, u=0] for all b in B
    if u == 0:
        alphas[offset] = 0

    # sync until all alphas are initialized
    cuda.syncthreads()

    # Ordinary alpha calculations, broadcast across B=b and U=u
    # Look up forward variable calculation from rnnt_numpy.forward_pass()
    for n in range(1, T + U - 1):
        t = n - u

        if u == 0:
            # for t in range(1, T) step to initialize alphas[b, t, 0]
            if t > 0 and t < T:
                alphas[offset + t * maxU + u] = alphas[
                    offset + (t - 1) * maxU + u
                ] + logp(denom, acts, maxT, maxU, alphabet_size, b, t - 1, 0, blank_)
        elif u < U:
            # for u in range(1, U) step to initialize alphas[b, 0, u]
            if t == 0:
                alphas[offset + u] = alphas[offset + u - 1] + logp(
                    denom, acts, maxT, maxU, alphabet_size, b, 0, u - 1, labels[u - 1]
                )

            # for t in range(1, T) for u in range(1, U) step to compute alphas[b, t, u]
            elif t > 0 and t < T:
                no_emit = alphas[offset + (t - 1) * maxU + u] + logp(
                    denom, acts, maxT, maxU, alphabet_size, b, t - 1, u, blank_
                )
                emit = alphas[offset + t * maxU + u - 1] + logp(
                    denom, acts, maxT, maxU, alphabet_size, b, t, u - 1, labels[u - 1]
                )

                alphas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(emit, no_emit)

        # sync across all B=b and U=u
        cuda.syncthreads()

    # After final sync, alphas[b, T-1, U - 1] + logprobs[b, T-1, U-1, blank] + denom[b, T-1, U-1] gives
    # log-likelihood of forward pass.
    if u == 0:
        loglike = alphas[offset + (T - 1) * maxU + U - 1] + logp(
            denom, acts, maxT, maxU, alphabet_size, b, T - 1, U - 1, blank_
        )
        llForward[b] = loglike


@cuda.jit()
def compute_betas_kernel(
    acts: torch.Tensor,
    denom: torch.Tensor,
    betas: torch.Tensor,
    llBackward: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B, U]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
):
    """
    Compute beta (backward variable) probabilities over the transduction step.

    Args:
        acts: Tensor of shape [B, T, U, V+1] flattened. Represents the logprobs activation tensor.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        betas: Zero tensor of shape [B, T, U]. Will be updated inside the kernel with the backward variable
            probabilities.
        llBackward: Zero tensor of shape [B]. Represents the log-likelihood of the backward pass.
            Returned as the backward pass loss that is reduced by the optimizer.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT blank token in the vocabulary. Generally the first or last token in the vocab.

    Updates:
        Kernel inplace updates the following inputs:
        -   betas: backward variable scores.
        -   llBackward: log-likelihood of backward variable.
    """
    # // launch B blocks, each block has U threads
    b = cuda.blockIdx.x  # // batch id
    u = cuda.threadIdx.x  # label id, u
    T = xlen[b]  # select AM length of current sample
    U = ylen[b] + 1  # select target length of current sample, +1 for the blank token

    labels: torch.Tensor = mlabels[
        b
    ]  # mb label start point, equivalent to mlabels + b * (maxU - 1)
    offset = b * maxT * maxU  # pointer indexing offset

    # betas += offset # pointer offset, ignored since we explicitly add offset

    # Initilize beta[b, t=T-1, u=U-1] for all b in B with log_probs[b, t=T-1, u=U-1, blank]
    if u == 0:
        betas[offset + (T - 1) * maxU + U - 1] = logp(
            denom, acts, maxT, maxU, alphabet_size, b, T - 1, U - 1, blank_
        )

    # sync until all betas are initialized
    cuda.syncthreads()

    # Ordinary beta calculations, broadcast across B=b and U=u
    # Look up backward variable calculation from rnnt_numpy.backward_pass()
    for n in range(T + U - 2, -1, -1):
        t = n - u

        if u == (U - 1):
            # for t in reversed(range(T - 1)) step to initialize betas[b, t, U-1]
            if t >= 0 and t < (T - 1):
                betas[offset + t * maxU + U - 1] = betas[
                    offset + (t + 1) * maxU + U - 1
                ] + logp(denom, acts, maxT, maxU, alphabet_size, b, t, U - 1, blank_)
        elif u < U:
            if t == T - 1:
                # for u in reversed(range(U - 1)) step to initialize betas[b, T-1, u]
                betas[offset + (T - 1) * maxU + u] = betas[
                    offset + (T - 1) * maxU + u + 1
                ] + logp(denom, acts, maxT, maxU, alphabet_size, b, T - 1, u, labels[u])
            elif (t >= 0) and (t < T - 1):
                # for t in reversed(range(T - 1)) for u in reversed(range(U - 1)) step to compute betas[b, t, u]
                no_emit = betas[offset + (t + 1) * maxU + u] + logp(
                    denom, acts, maxT, maxU, alphabet_size, b, t, u, blank_
                )
                emit = betas[offset + t * maxU + u + 1] + logp(
                    denom, acts, maxT, maxU, alphabet_size, b, t, u, labels[u]
                )
                betas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(emit, no_emit)

        # sync across all B=b and U=u
        cuda.syncthreads()

    # After final sync, betas[b, 0, 0] gives
    # log-likelihood of backward pass.
    if u == 0:
        llBackward[b] = betas[offset]


@cuda.jit()
def compute_grad_kernel(
    grads: torch.Tensor,
    acts: torch.Tensor,
    denom: torch.Tensor,
    alphas: torch.Tensor,
    betas: torch.Tensor,
    logll: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B, U]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
    fastemit_lambda: float,
    clamp: float,
):
    """
    Compute gradients over the transduction step.

    Args:
        grads: Zero Tensor of shape [B, T, U, V+1]. Is updated by this kernel to contain the gradients
            of this batch of samples.
        acts: Tensor of shape [B, T, U, V+1] flattened. Represents the logprobs activation tensor.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        alphas: Alpha variable, contains forward probabilities. A tensor of shape [B, T, U].
        betas: Beta varoable, contains backward probabilities. A tensor of shape [B, T, U].
        logll: Log-likelihood of the forward variable, represented as a vector of shape [B].
            Represents the log-likelihood of the forward pass.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT blank token in the vocabulary. Generally the first or last token in the vocab.
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
            FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        clamp: Float value. When set to value >= 0.0, will clamp the gradient to [-clamp, clamp].

    Updates:
        Kernel inplace updates the following inputs:
        -   grads: Gradients with respect to the log likelihood (logll).
    """
    # Kernel call:
    # blocks_per_grid = minibatch (b) * maxT (t) * maxU (u)
    # threads_per_block = constant buffer size of parallel threads (v :: Constant)
    tid = cuda.threadIdx.x  # represents v, taking steps of some constant size
    idx = tid  # index of v < V+1; in steps of constant buffer size
    col = cuda.blockIdx.x  # represents a fused index of b * t * u

    # Decompose original indices from fused `col`
    u = col % maxU  # (b * t * u) % u = u
    bt = (col - u) // maxU  # (b * t * u - u) // U = b * t
    t = bt % maxT  # (b * t) % t = t
    mb = (bt - t) // maxT  # (b * t - t) // T = b

    # constants
    T = xlen[mb]  # select AM length of current sample
    U = ylen[mb] + 1  # select target length of current sample, +1 for the blank token
    labels: torch.Tensor = mlabels[mb]  # labels = mlabels + mb * (maxU - 1);

    # Buffered gradient calculations, broadcast across B=b, T=t and U=u, looped over V with some constant stride.
    # Look up gradient calculation from rnnt_numpy.compute_gradient()
    if t < T and u < U:
        # For cuda kernels, maximum number of threads per block is limited to some value.
        # However, it may be the case that vocabulary size is larger than this limit
        # To work around this, an arbitrary thread buffer size is chosen such that,
        # 1) each element within the thread pool operates independently of the other
        # 2) An inner while loop moves the index of each buffer element by the size of the buffer itself,
        #    such that all elements of the vocabulary size are covered in (V + 1 // thread_buffer) number of steps.
        # As such, each thread will perform the while loop at least (V + 1 // thread_buffer) number of times
        while idx < alphabet_size:
            # remember, `col` represents the tri-index [b, t, u]
            # therefore; logpk = denom[b, t, u] + acts[b, t, u, v]
            logpk = denom[col] + acts[col * alphabet_size + idx]
            # initialize the grad of the sample acts[b, t, u, v]
            grad = math.exp(alphas[col] + betas[col] + logpk - logll[mb])

            # If FastEmit regularization is enabled, calculate the gradeint of probability of predicting the next label
            # at the current timestep.
            # The formula for this is Equation 9 in https://arxiv.org/abs/2010.11148, multiplied by the log probability
            # of the current step (t, u), normalized by the total log likelihood.
            # Once the gradient has been calculated, scale it by `fastemit_lambda`, as in Equation 10.
            if fastemit_lambda > 0.0 and u < U - 1:
                fastemit_grad = fastemit_lambda * math.exp(
                    alphas[col]  # alphas(t, u)
                    + (
                        denom[col] + acts[col * alphabet_size + labels[u]]
                    )  # y_hat(t, u)
                    + betas[col + 1]  # betas(t, u+1)
                    + logpk  # log Pr(k|t, u)
                    - logll[mb]  # total log likelihood for normalization
                )
            else:
                fastemit_grad = 0.0

            # Update the gradient of act[b, t, u, v] with the gradient from FastEmit regularization
            grad = grad + fastemit_grad

            # // grad to last blank transition
            # grad[b, T-1, U-1, v=blank] -= exp(alphas[b, t, u) + logpk - logll[b])
            if (idx == blank_) and (t == T - 1) and (u == U - 1):
                grad -= math.exp(alphas[col] + logpk - logll[mb])

            # grad of blank across t < T;
            # grad[b, t<T-1, u, v=blank] -= exp(alphas[b, t, u] + logpk - logll[b] betas[b, t + 1, u])
            if (idx == blank_) and (t < T - 1):
                grad -= math.exp(alphas[col] + logpk - logll[mb] + betas[col + maxU])

            # grad of correct token across u < U;
            # grad[b, t, u<U-1, v=label[u]] -= exp(alphas[b, t, u] + logpk - logll[b] + betas[b, t, u+1])
            # Scale the gradient by (1.0 + FastEmit_lambda) in log space, then exponentiate
            if (u < U - 1) and (idx == labels[u]):
                # exp(log(1 + fastemit_lambda) + ...) is numerically more stable than
                # multiplying (1.0 + fastemit_lambda) with result.
                grad -= math.exp(
                    math.log1p(fastemit_lambda)
                    + alphas[col]
                    + logpk
                    - logll[mb]
                    + betas[col + 1]
                )

            # update grads[b, t, u, v] = grad
            grads[col * alphabet_size + idx] = grad

            # clamp gradient (if needed)
            if clamp > 0.0:
                g = grads[col * alphabet_size + idx]
                g = min(g, clamp)
                g = max(g, -clamp)
                grads[col * alphabet_size + idx] = g

            # update internal index through the thread_buffer;
            # until idx < V + 1, such that entire vocabulary has been updated.
            idx += GPU_RNNT_THREAD_SIZE


@cuda.jit()
def compute_multiblank_alphas_kernel(
    acts: torch.Tensor,
    denom: torch.Tensor,
    sigma: float,
    alphas: torch.Tensor,
    llForward: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
    big_blank_duration: torch.Tensor,
    num_big_blanks: int,
):
    """
    Compute alpha (forward variable) probabilities for multi-blank transducuer loss (https://arxiv.org/pdf/2211.03541).

    Args:
        acts: Tensor of shape [B, T, U, V + 1 + num_big_blanks] flattened. Represents the logprobs activation tensor.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        sigma: Hyper-parameter for logit-undernormalization technique for training multi-blank transducers.
        alphas: Zero tensor of shape [B, T, U]. Will be updated inside the kernel with the forward variable
            probabilities.
        llForward: Zero tensor of shape [B]. Represents the log-likelihood of the forward pass.
            Returned as the forward pass loss that is reduced by the optimizer.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT standard blank token in the vocabulary.
        big_blank_durations: Vector of supported big blank durations of the model.
        num_big_blanks: Number of big blanks of the model.

    Updates:
        Kernel inplace updates the following inputs:
        -   alphas: forward variable scores.
        -   llForward: log-likelihood of forward variable.
    """
    # // launch B blocks, each block has U threads
    b = cuda.blockIdx.x  # // batch id
    u = cuda.threadIdx.x  # label id, u
    T = xlen[b]  # select AM length of current sample
    U = ylen[b] + 1  # select target length of current sample, +1 for the blank token

    labels: torch.Tensor = mlabels[
        b
    ]  # mb label start point, equivalent to mlabels + b * (maxU - 1)
    offset = b * maxT * maxU  # pointer indexing offset

    # Initilize alpha[b, t=0, u=0] for all b in B
    if u == 0:
        alphas[offset] = 0

    # sync until all alphas are initialized
    cuda.syncthreads()

    # Ordinary alpha calculations, broadcast across B=b and U=u
    # Look up forward variable calculation from rnnt_numpy.forward_pass()
    # Note: because of the logit under-normalization, everytime logp() is called,
    # it is always followed by a `-sigma` term.
    for n in range(1, T + U - 1):
        t = n - u

        if u == 0:
            # for t in range(1, T) step to initialize alphas[b, t, 0]
            if t > 0 and t < T:
                alphas[offset + t * maxU + u] = (
                    alphas[offset + (t - 1) * maxU + u]
                    + logp(denom, acts, maxT, maxU, alphabet_size, b, t - 1, 0, blank_)
                    - sigma
                )

                # Now add the weights for big blanks.
                for i in range(num_big_blanks):
                    if t >= big_blank_duration[i]:
                        alphas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(
                            alphas[offset + t * maxU + u],
                            alphas[offset + (t - big_blank_duration[i]) * maxU + u]
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t - big_blank_duration[i],
                                0,
                                blank_ - 1 - i,
                            )
                            - sigma,
                        )

        elif u < U:
            # for u in range(1, U) step to initialize alphas[b, 0, u]
            if t == 0:
                alphas[offset + u] = (
                    alphas[offset + u - 1]
                    + logp(
                        denom,
                        acts,
                        maxT,
                        maxU,
                        alphabet_size,
                        b,
                        0,
                        u - 1,
                        labels[u - 1],
                    )
                    - sigma
                )

            # for t in range(1, T) for u in range(1, U) step to compute alphas[b, t, u]
            elif t > 0 and t < T:
                no_emit = (
                    alphas[offset + (t - 1) * maxU + u]
                    + logp(denom, acts, maxT, maxU, alphabet_size, b, t - 1, u, blank_)
                    - sigma
                )
                emit = (
                    alphas[offset + t * maxU + u - 1]
                    + logp(
                        denom,
                        acts,
                        maxT,
                        maxU,
                        alphabet_size,
                        b,
                        t,
                        u - 1,
                        labels[u - 1],
                    )
                    - sigma
                )

                alphas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(emit, no_emit)

                # Now add the weights for big blanks.
                for i in range(num_big_blanks):
                    if t >= big_blank_duration[i]:
                        # big-blank weight here is
                        # alpha(t - duration, u) * p(big-blank | t - duration, u) / exp(sigma), in log domain
                        # do this all all big-blanks if the above condition is met
                        big_blank_no_emit = (
                            alphas[offset + (t - big_blank_duration[i]) * maxU + u]
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t - big_blank_duration[i],
                                u,
                                blank_ - 1 - i,
                            )
                            - sigma
                        )
                        alphas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(
                            alphas[offset + t * maxU + u], big_blank_no_emit
                        )

        # sync across all B=b and U=u
        cuda.syncthreads()

    # After final sync, alphas[b, T-1, U - 1] + logprobs[b, T-1, U-1, blank] + denom[b, T-1, U-1] gives
    # log-likelihood of forward pass.
    if u == 0:
        loglike = (
            alphas[offset + (T - 1) * maxU + U - 1]
            + logp(denom, acts, maxT, maxU, alphabet_size, b, T - 1, U - 1, blank_)
            - sigma
        )

        # Now add the weights for big blanks for the final weight computation.
        for i in range(num_big_blanks):
            if T >= big_blank_duration[i]:
                big_blank_loglike = (
                    alphas[offset + (T - big_blank_duration[i]) * maxU + U - 1]
                    + logp(
                        denom,
                        acts,
                        maxT,
                        maxU,
                        alphabet_size,
                        b,
                        T - big_blank_duration[i],
                        U - 1,
                        blank_ - 1 - i,
                    )
                    - sigma
                )
                loglike = rnnt_helper.log_sum_exp(loglike, big_blank_loglike)

        llForward[b] = loglike


@cuda.jit()
def compute_multiblank_betas_kernel(
    acts: torch.Tensor,
    denom: torch.Tensor,
    sigma: float,
    betas: torch.Tensor,
    llBackward: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B, U]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
    big_blank_duration: torch.Tensor,
    num_big_blanks: int,
):
    """
    Compute beta (backward variable) probabilities for multi-blank transducer loss (https://arxiv.org/pdf/2211.03541).

    Args:
        acts: Tensor of shape [B, T, U, V + 1 + num-big-blanks] flattened. Represents the logprobs activation tensor.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        sigma: Hyper-parameter for logit-undernormalization technique for training multi-blank transducers.
        betas: Zero tensor of shape [B, T, U]. Will be updated inside the kernel with the backward variable
            probabilities.
        llBackward: Zero tensor of shape [B]. Represents the log-likelihood of the backward pass.
            Returned as the backward pass loss that is reduced by the optimizer.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT standard blank token in the vocabulary.
        big_blank_durations: Vector of supported big blank durations of the model.
        num_big_blanks: Number of big blanks of the model.

    Updates:
        Kernel inplace updates the following inputs:
        -   betas: backward variable scores.
        -   llBackward: log-likelihood of backward variable.
    """
    # // launch B blocks, each block has U threads
    b = cuda.blockIdx.x  # // batch id
    u = cuda.threadIdx.x  # label id, u
    T = xlen[b]  # select AM length of current sample
    U = ylen[b] + 1  # select target length of current sample, +1 for the blank token

    labels: torch.Tensor = mlabels[
        b
    ]  # mb label start point, equivalent to mlabels + b * (maxU - 1)
    offset = b * maxT * maxU  # pointer indexing offset

    # Note: just like the alphas, because of the logit under-normalization, everytime
    # logp() is called, it is always followed by a `-sigma` term.

    # Initilize beta[b, t=T-1, u=U-1] for all b in B with log_probs[b, t=T-1, u=U-1, blank]
    if u == 0:
        betas[offset + (T - 1) * maxU + U - 1] = (
            logp(denom, acts, maxT, maxU, alphabet_size, b, T - 1, U - 1, blank_)
            - sigma
        )

    # sync until all betas are initialized
    cuda.syncthreads()

    # Ordinary beta calculations, broadcast across B=b and U=u
    # Look up backward variable calculation from rnnt_numpy.backward_pass()
    for n in range(T + U - 2, -1, -1):
        t = n - u

        if u == (U - 1):
            # for t in reversed(range(T - 1)) step to initialize betas[b, t, U-1]
            if t >= 0 and t < (T - 1):
                # beta[t, U - 1] = beta[t + 1, U - 1] * p(blank | t, U - 1) / exp(sigma)
                # this part is the same as regular RNN-T.
                betas[offset + t * maxU + U - 1] = (
                    betas[offset + (t + 1) * maxU + U - 1]
                    + logp(denom, acts, maxT, maxU, alphabet_size, b, t, U - 1, blank_)
                    - sigma
                )

                # now add the weights from big blanks
                for i in range(num_big_blanks):
                    if t + big_blank_duration[i] < T:
                        # adding to beta[t, U - 1] of weight (in log domain),
                        # beta[t + duration, U - 1] * p(big-blank | t, U - 1) / exp(sigma)
                        betas[offset + t * maxU + U - 1] = rnnt_helper.log_sum_exp(
                            betas[offset + t * maxU + U - 1],
                            betas[offset + (t + big_blank_duration[i]) * maxU + U - 1]
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t,
                                U - 1,
                                blank_ - 1 - i,
                            )
                            - sigma,
                        )
                    elif t + big_blank_duration[i] == T and big_blank_duration[i] != 1:
                        # adding to beta[T - duration, U - 1] of weight (in log domain),
                        # p(big-blank | T - duration, U - 1) / exp(sigma)
                        betas[offset + t * maxU + U - 1] = rnnt_helper.log_sum_exp(
                            betas[offset + t * maxU + U - 1],
                            logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t,
                                U - 1,
                                blank_ - 1 - i,
                            )
                            - sigma,
                        )

        elif u < U:
            if t == T - 1:
                # for u in reversed(range(U - 1)) step to initialize betas[b, T-1, u]
                betas[offset + (T - 1) * maxU + u] = (
                    betas[offset + (T - 1) * maxU + u + 1]
                    + logp(
                        denom, acts, maxT, maxU, alphabet_size, b, T - 1, u, labels[u]
                    )
                    - sigma
                )
            elif (t >= 0) and (t < T - 1):
                # for t in reversed(range(T - 1)) for u in reversed(range(U - 1)) step to compute betas[b, t, u]
                no_emit = (
                    betas[offset + (t + 1) * maxU + u]
                    + logp(denom, acts, maxT, maxU, alphabet_size, b, t, u, blank_)
                    - sigma
                )
                emit = (
                    betas[offset + t * maxU + u + 1]
                    + logp(denom, acts, maxT, maxU, alphabet_size, b, t, u, labels[u])
                    - sigma
                )
                betas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(emit, no_emit)

                # now add the weights from big blanks
                for i in range(num_big_blanks):
                    if t < T - big_blank_duration[i]:
                        # added weight for the big-blank,
                        # beta[t + duration, u] * p(big-blank | t, u) / exp(sigma)
                        big_blank_no_emit = (
                            betas[offset + (t + big_blank_duration[i]) * maxU + u]
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t,
                                u,
                                blank_ - 1 - i,
                            )
                            - sigma
                        )
                        betas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(
                            betas[offset + t * maxU + u], big_blank_no_emit
                        )

        # sync across all B=b and U=u
        cuda.syncthreads()

    # After final sync, betas[b, 0, 0] gives
    # log-likelihood of backward pass.
    if u == 0:
        llBackward[b] = betas[offset]


@cuda.jit()
def compute_multiblank_grad_kernel(
    grads: torch.Tensor,
    acts: torch.Tensor,
    denom: torch.Tensor,
    sigma: float,
    alphas: torch.Tensor,
    betas: torch.Tensor,
    logll: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B, U]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
    big_blank_duration: torch.Tensor,
    num_big_blanks: int,
    fastemit_lambda: float,
    clamp: float,
):
    """
    Compute gradients for multi-blank transducer loss (https://arxiv.org/pdf/2211.03541).

    Args:
        grads: Zero Tensor of shape [B, T, U, V + 1 + num_big_blanks]. Is updated by this kernel to contain the gradients
            of this batch of samples.
        acts: Tensor of shape [B, T, U, V + 1 + num_big_blanks] flattened. Represents the logprobs activation tensor.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        sigma: Hyper-parameter for logit-undernormalization technique for training multi-blank transducers.
        alphas: Alpha variable, contains forward probabilities. A tensor of shape [B, T, U].
        betas: Beta varoable, contains backward probabilities. A tensor of shape [B, T, U].
        logll: Log-likelihood of the forward variable, represented as a vector of shape [B].
            Represents the log-likelihood of the forward pass.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT blank token in the vocabulary. Generally the first or last token in the vocab.
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
            FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        clamp: Float value. When set to value >= 0.0, will clamp the gradient to [-clamp, clamp].
        big_blank_durations: Vector of supported big blank durations of the model.
        num_big_blanks: Number of big blanks of the model.

    Updates:
        Kernel inplace updates the following inputs:
        -   grads: Gradients with respect to the log likelihood (logll).
    """
    # Kernel call:
    # blocks_per_grid = minibatch (b) * maxT (t) * maxU (u)
    # threads_per_block = constant buffer size of parallel threads (v :: Constant)
    tid = cuda.threadIdx.x  # represents v, taking steps of some constant size
    idx = tid  # index of v < V+1; in steps of constant buffer size
    col = cuda.blockIdx.x  # represents a fused index of b * t * u

    # Decompose original indices from fused `col`
    u = col % maxU  # (b * t * u) % u = u
    bt = (col - u) // maxU  # (b * t * u - u) // U = b * t
    t = bt % maxT  # (b * t) % t = t
    mb = (bt - t) // maxT  # (b * t - t) // T = b

    # constants
    T = xlen[mb]  # select AM length of current sample
    U = ylen[mb] + 1  # select target length of current sample, +1 for the blank token
    labels: torch.Tensor = mlabels[mb]  # labels = mlabels + mb * (maxU - 1);

    # Buffered gradient calculations, broadcast across B=b, T=t and U=u, looped over V with some constant stride.
    # Look up gradient calculation from rnnt_numpy.compute_gradient()
    if t < T and u < U:
        # For cuda kernels, maximum number of threads per block is limited to some value.
        # However, it may be the case that vocabulary size is larger than this limit
        # To work around this, an arbitrary thread buffer size is chosen such that,
        # 1) each element within the thread pool operates independently of the other
        # 2) An inner while loop moves the index of each buffer element by the size of the buffer itself,
        #    such that all elements of the vocabulary size are covered in (V + 1 // thread_buffer) number of steps.
        # As such, each thread will perform the while loop at least (V + 1 // thread_buffer) number of times
        while idx < alphabet_size:
            # remember, `col` represents the tri-index [b, t, u]
            # therefore; logpk = denom[b, t, u] + acts[b, t, u, v]
            logpk = denom[col] + acts[col * alphabet_size + idx]
            # initialize the grad of the sample acts[b, t, u, v]
            grad = math.exp(alphas[col] + betas[col] + logpk - logll[mb])

            # In all of the following computation, whenever logpk is used, we
            # need to subtract sigma based on our derivation of the gradient of
            # the logit under-normalization method.

            # If FastEmit regularization is enabled, calculate the gradeint of probability of predicting the next label
            # at the current timestep.
            # The formula for this is Equation 9 in https://arxiv.org/abs/2010.11148, multiplied by the log probability
            # of the current step (t, u), normalized by the total log likelihood.
            # Once the gradient has been calculated, scale it by `fastemit_lambda`, as in Equation 10.
            if fastemit_lambda > 0.0 and u < U - 1:
                fastemit_grad = fastemit_lambda * math.exp(
                    alphas[col]  # alphas(t, u)
                    + (denom[col] + acts[col * alphabet_size + labels[u]])
                    + betas[col + 1]  # betas(t, u+1)
                    + logpk  # log Pr(k|t, u)
                    - sigma
                    - logll[mb]  # total log likelihood for normalization
                )
            else:
                fastemit_grad = 0.0

            # Update the gradient of act[b, t, u, v] with the gradient from FastEmit regularization
            grad = grad + fastemit_grad

            # grad to last blank transition
            # grad[b, T-1, U-1, v=blank] -= exp(alphas[b, t, u) + logpk - sigma - logll[b])
            if (idx == blank_) and (t == T - 1) and (u == U - 1):
                grad -= math.exp(alphas[col] + logpk - sigma - logll[mb])
            else:
                # this is one difference of the multi-blank gradient from standard RNN-T
                # gradient -- basically, wherever the blank_ symbol is addressed in the
                # original code, we need to do similar things to big blanks, and we need
                # to change the if conditions to match the duration of the big-blank.
                # grad[b, T-duration, U-1, v=big-blank] -= exp(alphas[b, t, u) + logpk - sigma - logll[b])
                for i in range(num_big_blanks):
                    if (
                        (idx == blank_ - 1 - i)
                        and (t == T - big_blank_duration[i])
                        and (u == U - 1)
                    ):
                        grad -= math.exp(alphas[col] + logpk - sigma - logll[mb])

            # grad of blank across t < T;
            # grad[b, t<T-1, u, v=blank] -= exp(alphas[b, t, u] + logpk - sigma - logll[b] betas[b, t + 1, u])
            if (idx == blank_) and (t < T - 1):
                grad -= math.exp(
                    alphas[col] + logpk - sigma - logll[mb] + betas[col + maxU]
                )
            else:
                # This is another difference between multi-blank and RNN-T gradients.
                # Now we consider gradients for big-blanks.
                # grad[b, t<T-duration, u, v=big-blank] -= exp(alphas[b, t, u] + logpk - sigma - logll[b] + betas[b, t + duration, u])
                for i in range(num_big_blanks):
                    if (idx == blank_ - 1 - i) and (t < T - big_blank_duration[i]):
                        grad -= math.exp(
                            alphas[col]
                            + logpk
                            - sigma
                            - logll[mb]
                            + betas[col + big_blank_duration[i] * maxU]
                        )

            # grad of correct token across u < U;
            # grad[b, t, u<U-1, v=label[u]] -= exp(alphas[b, t, u] + logpk - sigma - logll[b] + betas[b, t, u+1])
            # Scale the gradient by (1.0 + FastEmit_lambda) in log space, then exponentiate
            if (u < U - 1) and (idx == labels[u]):
                # exp(log(1 + fastemit_lambda) + ...) is numerically more stable than
                # multiplying (1.0 + fastemit_lambda) with result.
                grad -= math.exp(
                    math.log1p(fastemit_lambda)
                    + alphas[col]
                    + logpk
                    - sigma
                    - logll[mb]
                    + betas[col + 1]
                )

            # update grads[b, t, u, v] = grad
            grads[col * alphabet_size + idx] = grad

            # clamp gradient (if needed)
            if clamp > 0.0:
                g = grads[col * alphabet_size + idx]
                g = min(g, clamp)
                g = max(g, -clamp)
                grads[col * alphabet_size + idx] = g

            # update internal index through the thread_buffer;
            # until idx < V + 1, such that entire vocabulary has been updated.
            idx += GPU_RNNT_THREAD_SIZE


@cuda.jit()
def compute_tdt_alphas_kernel(
    acts: torch.Tensor,
    duration_acts: torch.Tensor,
    denom: torch.Tensor,
    sigma: float,
    alphas: torch.Tensor,
    llForward: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
    durations: torch.Tensor,
    num_durations: int,
):
    """
    Compute alpha (forward variable) probabilities over the transduction step.

    Args:
        acts: Tensor of shape [B, T, U, V] flattened. Represents the logprobs activation tensor for tokens.
        duration_acts: Tensor of shape [B, T, U, D] flattened. Represents the logprobs activation tensor for duration.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor for tokens.

        alphas: Zero tensor of shape [B, T, U]. Will be updated inside the kernel with the forward variable
            probabilities.
        llForward: Zero tensor of shape [B]. Represents the log-likelihood of the forward pass.
            Returned as the forward pass loss that is reduced by the optimizer.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the TDT blank token in the vocabulary. Must be the last token in the vocab.

    Updates:
        Kernel inplace updates the following inputs:
        -   alphas: forward variable scores.
        -   llForward: log-likelihood of forward variable.
    """
    # // launch B blocks, each block has U threads
    b = cuda.blockIdx.x  # // batch id
    u = cuda.threadIdx.x  # label id, u
    T = xlen[b]  # select AM length of current sample
    U = ylen[b] + 1  # select target length of current sample, +1 for the blank token

    labels: torch.Tensor = mlabels[
        b
    ]  # mb label start point, equivalent to mlabels + b * (maxU - 1)
    offset = b * maxT * maxU  # pointer indexing offset

    # alphas += offset # pointer offset, ignored since we explicitly add offset

    # Initilize alpha[b, t=0, u=0] for all b in B
    if u == 0:
        alphas[offset] = 0

    # sync until all alphas are initialized
    cuda.syncthreads()

    # Ordinary alpha calculations, broadcast across B=b and U=u
    # Look up forward variable calculation from rnnt_numpy.forward_pass()
    for n in range(1, T + U - 1):
        t = n - u

        if u == 0:
            # when u == 0, we only consider blank emissions.
            if t > 0 and t < T:
                alphas[offset + t * maxU + u] = -INF

                for i in range(num_durations):
                    if (
                        durations[i] == 0
                    ):  # skip 0 since blank emission has to advance by at least one
                        continue
                    if t >= durations[i]:
                        alphas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(
                            alphas[offset + t * maxU + u],  # the current alpha value
                            alphas[
                                offset + (t - durations[i]) * maxU + u
                            ]  # alpha(t - duration, u)
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t - durations[i],
                                u,
                                blank_,
                            )  # logp of blank emission
                            - sigma  #  logit under-normalization
                            + logp_duration(
                                duration_acts,
                                maxT,
                                maxU,
                                num_durations,
                                b,
                                t - durations[i],
                                u,
                                i,
                            ),  # logp of duration
                        )
                    else:
                        break  # since durations are in ascending order, when we encounter a duration that is too large, then
                        # there is no need to check larger durations after that.

        elif u < U:
            # when t == 0, we only consider the non-blank emission.
            if t == 0:
                if durations[0] == 0:
                    alphas[offset + u] = (
                        alphas[offset + u - 1]  # alpha(t, u - 1)
                        + logp(
                            denom,
                            acts,
                            maxT,
                            maxU,
                            alphabet_size,
                            b,
                            t,
                            u - 1,
                            labels[u - 1],
                        )  # logp of token emission
                        - sigma  # logit under-normalization
                        + logp_duration(
                            duration_acts, maxT, maxU, num_durations, b, t, u - 1, 0
                        )  # t = 0, so it must be duration = 0. Therefore the last argument passed to logp_duration() is 0.
                    )
                else:
                    alphas[offset + u] = -INF

            # now we have t != 0 and u != 0, and we need to consider both non-blank and blank emissions.
            elif t > 0 and t < T:
                no_emit = -INF  # no_emit stores the score for all blank emissions.
                for i in range(num_durations):
                    if durations[i] == 0:
                        continue
                    if t >= durations[i]:
                        no_emit = rnnt_helper.log_sum_exp(
                            no_emit,  # current score
                            alphas[
                                offset + (t - durations[i]) * maxU + u
                            ]  # alpha(t - duration, u)
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t - durations[i],
                                u,
                                blank_,
                            )  # logp of blank emission
                            - sigma  #  logit under-normalization
                            + logp_duration(
                                duration_acts,
                                maxT,
                                maxU,
                                num_durations,
                                b,
                                t - durations[i],
                                u,
                                i,
                            ),  # logp of duration
                        )
                    else:
                        break  # we can exit the loop early here, same as the case for u == 0 above.

                emit = -INF  # emit stores the score for non-blank emissions.
                for i in range(num_durations):
                    if t >= durations[i]:
                        emit = rnnt_helper.log_sum_exp(
                            emit,  # current score
                            alphas[
                                offset + (t - durations[i]) * maxU + u - 1
                            ]  # alpha(t - duration, u - 1)
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t - durations[i],
                                u - 1,
                                labels[u - 1],
                            )  # logp of non-blank emission
                            - sigma  #  logit under-normalization
                            + logp_duration(
                                duration_acts,
                                maxT,
                                maxU,
                                num_durations,
                                b,
                                t - durations[i],
                                u - 1,
                                i,
                            ),  # logp of duration
                        )
                    else:
                        break  # we can exit the loop early here, same as the case for u == 0 above.

                # combining blank and non-blank emissions.
                alphas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(emit, no_emit)

        # sync across all B=b and U=u
        cuda.syncthreads()

    # After final sync, the forward log-likelihood can be computed as the summataion of
    # alpha(T - duration, U - 1) + logp(blank, duration | t - duration, U - 1), over different durations.
    if u == 0:
        # initialize with negative infinite and start add terms later
        loglike = -INF

        # then we add the scores for duration > 1, if such durations are possible given the audio lengths.
        for i in range(num_durations):
            if durations[i] == 0:
                continue
            if durations[i] == 1:
                loglike = (
                    alphas[offset + (T - 1) * maxU + U - 1]
                    + logp(
                        denom, acts, maxT, maxU, alphabet_size, b, T - 1, U - 1, blank_
                    )
                    - sigma
                    + logp_duration(
                        duration_acts, maxT, maxU, num_durations, b, T - 1, U - 1, i
                    )
                )
                continue
            if T >= durations[i]:
                big_blank_loglike = (
                    alphas[offset + (T - durations[i]) * maxU + U - 1]
                    + logp(
                        denom,
                        acts,
                        maxT,
                        maxU,
                        alphabet_size,
                        b,
                        T - durations[i],
                        U - 1,
                        blank_,
                    )
                    - sigma
                    + logp_duration(
                        duration_acts,
                        maxT,
                        maxU,
                        num_durations,
                        b,
                        T - durations[i],
                        U - 1,
                        i,
                    )
                )
                loglike = rnnt_helper.log_sum_exp(loglike, big_blank_loglike)
            else:
                break

        llForward[b] = loglike


@cuda.jit()
def compute_tdt_betas_kernel(
    acts: torch.Tensor,
    duration_acts: torch.Tensor,
    denom: torch.Tensor,
    sigma: float,
    betas: torch.Tensor,
    llBackward: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B, U]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
    durations: torch.Tensor,
    num_durations: int,
):
    """
    Compute beta (backward variable) probabilities over the transduction step.

    Args:
        acts: Tensor of shape [B, T, U, V] flattened. Represents the logprobs activation tensor for tokens.
        duration_acts: Tensor of shape [B, T, U, D] flattened. Represents the logprobs activation tensor for duations.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        betas: Zero tensor of shape [B, T, U]. Will be updated inside the kernel with the backward variable
            probabilities.
        llBackward: Zero tensor of shape [B]. Represents the log-likelihood of the backward pass.
            Returned as the backward pass loss that is reduced by the optimizer.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT blank token in the vocabulary. Generally the first or last token in the vocab.

    Updates:
        Kernel inplace updates the following inputs:
        -   betas: backward variable scores.
        -   llBackward: log-likelihood of backward variable.
    """
    # // launch B blocks, each block has U threads
    b = cuda.blockIdx.x  # // batch id
    u = cuda.threadIdx.x  # label id, u
    T = xlen[b]  # select AM length of current sample
    U = ylen[b] + 1  # select target length of current sample, +1 for the blank token

    labels: torch.Tensor = mlabels[
        b
    ]  # mb label start point, equivalent to mlabels + b * (maxU - 1)
    offset = b * maxT * maxU  # pointer indexing offset

    # betas += offset # pointer offset, ignored since we explicitly add offset

    # Initilize beta[b, t=T-1, u=U-1] for all b in B with log_probs[b, t=T-1, u=U-1, blank]
    if u == 0:
        if durations[0] == 1:
            betas[offset + (T - 1) * maxU + U - 1] = (
                logp(denom, acts, maxT, maxU, alphabet_size, b, T - 1, U - 1, blank_)
                - sigma
                + logp_duration(
                    duration_acts, maxT, maxU, num_durations, b, T - 1, U - 1, 0
                )
            )
        elif durations[1] == 1:
            betas[offset + (T - 1) * maxU + U - 1] = (
                logp(denom, acts, maxT, maxU, alphabet_size, b, T - 1, U - 1, blank_)
                - sigma
                + logp_duration(
                    duration_acts, maxT, maxU, num_durations, b, T - 1, U - 1, 1
                )
            )

    # sync until all betas are initialized
    cuda.syncthreads()

    # Ordinary beta calculations, broadcast across B=b and U=u
    # Look up backward variable calculation from rnnt_numpy.backward_pass()
    for n in range(T + U - 2, -1, -1):
        t = n - u

        if u == U - 1:
            # u == U - 1, we only consider blank emissions.
            if t >= 0 and t + 1 < T:
                betas[offset + t * maxU + U - 1] = -INF
                for i in range(num_durations):
                    # although similar, the computation for beta's is slightly more complex for boundary cases.
                    # the following two cases correspond to whether t is exactly certain duration away from T.
                    # and they have slightly different update rules.
                    if durations[i] == 0:
                        continue
                    if t + durations[i] < T:
                        betas[offset + t * maxU + U - 1] = rnnt_helper.log_sum_exp(
                            betas[offset + t * maxU + U - 1],
                            betas[
                                offset + (t + durations[i]) * maxU + U - 1
                            ]  # beta[t, U - 1] depends on the value beta[t + duration, U - 1] here.
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t,
                                U - 1,
                                blank_,
                            )  # log prob of blank
                            + logp_duration(
                                duration_acts, maxT, maxU, num_durations, b, t, U - 1, i
                            )  # log prob of duration (durations[i])
                            - sigma,  # for logit undernormalization
                        )
                    elif t + durations[i] == T:
                        betas[offset + t * maxU + U - 1] = rnnt_helper.log_sum_exp(
                            betas[offset + t * maxU + U - 1],
                            # here we have one fewer term than the "if" block above. This could be seen as having "0" here since
                            # beta[t + duration, U - 1] isn't defined because t + duration is out of bound.
                            logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t,
                                U - 1,
                                blank_,
                            )  # log prob of blank
                            + logp_duration(
                                duration_acts, maxT, maxU, num_durations, b, t, U - 1, i
                            )  # log prob of duration (durations[i])
                            - sigma,  # for logit undernormalization. Basically every time sigma shows up is because of logit undernormalization.
                        )

        elif u < U - 1:
            if t == T - 1:
                # t == T - 1, so we only consider non-blank with duration 0. (Note, we can't have blank emissions with duration = 0)
                if durations[0] == 0:
                    betas[offset + (T - 1) * maxU + u] = (
                        betas[offset + (T - 1) * maxU + u + 1]
                        + logp(
                            denom,
                            acts,
                            maxT,
                            maxU,
                            alphabet_size,
                            b,
                            T - 1,
                            u,
                            labels[u],
                        )  # non-blank log prob
                        + logp_duration(
                            duration_acts, maxT, maxU, num_durations, b, T - 1, u, 0
                        )  # log prob of duration 0
                        - sigma
                    )
                else:
                    betas[offset + (T - 1) * maxU + u] = -INF

            elif t >= 0 and t < T - 1:
                # now we need to consider both blank andnon-blanks. Similar to alphas, we first compute them separately with no_emit and emit.
                no_emit = -INF
                for i in range(num_durations):
                    if durations[i] == 0:
                        continue
                    if t + durations[i] < T:
                        no_emit = rnnt_helper.log_sum_exp(
                            no_emit,
                            betas[offset + (t + durations[i]) * maxU + u]
                            + logp(
                                denom, acts, maxT, maxU, alphabet_size, b, t, u, blank_
                            )
                            + logp_duration(
                                duration_acts, maxT, maxU, num_durations, b, t, u, i
                            )
                            - sigma,
                        )

                emit = -INF
                for i in range(num_durations):
                    if t + durations[i] < T:
                        emit = rnnt_helper.log_sum_exp(
                            emit,
                            betas[offset + (t + durations[i]) * maxU + u + 1]
                            + logp(
                                denom,
                                acts,
                                maxT,
                                maxU,
                                alphabet_size,
                                b,
                                t,
                                u,
                                labels[u],
                            )
                            + logp_duration(
                                duration_acts, maxT, maxU, num_durations, b, t, u, i
                            )
                            - sigma,
                        )

                # combining all blank emissions and all non-blank emissions.
                betas[offset + t * maxU + u] = rnnt_helper.log_sum_exp(emit, no_emit)

        # sync across all B=b and U=u
        cuda.syncthreads()

    # After final sync, betas[b, 0, 0] gives log-likelihood of backward pass, same with conventional Transducers.
    if u == 0:
        llBackward[b] = betas[offset]


@cuda.jit()
def compute_tdt_grad_kernel(
    label_grads: torch.Tensor,
    duration_grads: torch.Tensor,
    acts: torch.Tensor,
    duration_acts: torch.Tensor,
    denom: torch.Tensor,
    sigma: float,
    alphas: torch.Tensor,
    betas: torch.Tensor,
    logll: torch.Tensor,
    xlen: torch.Tensor,
    ylen: torch.Tensor,
    mlabels: torch.Tensor,  # [B, U]
    minibatch: int,
    maxT: int,
    maxU: int,
    alphabet_size: int,
    blank_: int,
    durations: torch.Tensor,
    num_durations: int,
    fastemit_lambda: float,
    clamp: float,
):
    """
    Compute gradients over the transduction step.

    Args:
        grads: Zero Tensor of shape [B, T, U, V] to store gradients for tokens.
        duration_grads: Zero Tensor of shape [B, T, U, D] to store gradients for durations.

        acts: Tensor of shape [B, T, U, V] flattened. Represents the logprobs activation tensor for tokens.
        duration_acts: Tensor of shape [B, T, U, D] flattened. Represents the logprobs activation tensor for durations.
        denom: Tensor of shape [B, T, U] flattened. Represents the denominator of the logprobs activation tensor
            across entire vocabulary.
        alphas: Alpha variable, contains forward probabilities. A tensor of shape [B, T, U].
        betas: Beta varoable, contains backward probabilities. A tensor of shape [B, T, U].
        logll: Log-likelihood of the forward variable, represented as a vector of shape [B].
            Represents the log-likelihood of the forward pass.
        xlen: Vector of length B which contains the actual acoustic sequence lengths in the padded
            activation tensor.
        ylen: Vector of length B which contains the actual target sequence lengths in the padded
            activation tensor.
        mlabels: Matrix of shape [B, U+1] (+1 here is due to <SOS> token - usually the RNNT blank).
            The matrix contains the padded target transcription that must be predicted.
        minibatch: Int representing the batch size.
        maxT: The maximum possible acoustic sequence length. Represents T in the logprobs tensor.
        maxU: The maximum possible target sequence length. Represents U in the logprobs tensor.
        alphabet_size: The vocabulary dimension V+1 (inclusive of RNNT blank).
        blank_: Index of the RNNT blank token in the vocabulary. Generally the first or last token in the vocab.
        fastemit_lambda: Float scaling factor for FastEmit regularization. Refer to
            FastEmit: Low-latency Streaming ASR with Sequence-level Emission Regularization.
        clamp: Float value. When set to value >= 0.0, will clamp the gradient to [-clamp, clamp].

    Updates:
        Kernel inplace updates the following inputs:
        -   grads: Gradients with respect to the log likelihood (logll).
    """
    # Kernel call:
    # blocks_per_grid = minibatch (b) * maxT (t) * maxU (u)
    # threads_per_block = constant buffer size of parallel threads (v :: Constant)
    tid = cuda.threadIdx.x  # represents v, taking steps of some constant size
    idx = tid  # index of v < V+1; in steps of constant buffer size
    col = cuda.blockIdx.x  # represents a fused index of b * t * u

    # Decompose original indices from fused `col`
    u = col % maxU  # (b * t * u) % u = u
    bt = (col - u) // maxU  # (b * t * u - u) // U = b * t
    t = bt % maxT  # (b * t) % t = t
    mb = (bt - t) // maxT  # (b * t - t) // T = b

    # constants
    T = xlen[mb]  # select AM length of current sample
    U = ylen[mb] + 1  # select target length of current sample, +1 for the blank token
    labels: torch.Tensor = mlabels[mb]  # labels = mlabels + mb * (maxU - 1);

    # Buffered gradient calculations, broadcast across B=b, T=t and U=u, looped over V with some constant stride.
    # Look up gradient calculation from rnnt_numpy.compute_gradient()

    if t < T and u < U:
        logpk_blank = (
            denom[col] + acts[col * alphabet_size + blank_] - sigma
        )  # whenever sigma is used, it is for logit under-normalization.

        if idx < num_durations:
            grad = 0.0
            if t + durations[idx] < T and u < U - 1:  # for label
                logpk_label = denom[col] + acts[col * alphabet_size + labels[u]] - sigma
                grad -= math.exp(
                    alphas[col]
                    + betas[col + 1 + durations[idx] * maxU]
                    + logpk_label
                    - logll[mb]
                )

            if t + durations[idx] < T and durations[idx] > 0:  # for blank in the middle
                grad -= math.exp(
                    alphas[col]
                    + betas[col + durations[idx] * maxU]
                    + logpk_blank
                    - logll[mb]
                )

            if (
                t + durations[idx] == T and u == U - 1 and durations[idx] > 0
            ):  # for blank as the last symbol
                grad -= math.exp(alphas[col] + logpk_blank - logll[mb])

            grad = grad * math.exp(duration_acts[col * num_durations + idx])
            duration_grads[col * num_durations + idx] = grad

        # For cuda kernels, maximum number of threads per block is limited to some value.
        # However, it may be the case that vocabulary size is larger than this limit
        # To work around this, an arbitrary thread buffer size is chosen such that,
        # 1) each element within the thread pool operates independently of the other
        # 2) An inner while loop moves the index of each buffer element by the size of the buffer itself,
        #    such that all elements of the vocabulary size are covered in (V + 1 // thread_buffer) number of steps.
        # As such, each thread will perform the while loop at least (V + 1 // thread_buffer) number of times
        while idx < alphabet_size:
            # remember, `col` represents the tri-index [b, t, u]
            # therefore; logpk = denom[b, t, u] + acts[b, t, u, v]
            logpk = denom[col] + acts[col * alphabet_size + idx]
            # initialize the grad of the sample acts[b, t, u, v]
            grad = math.exp(alphas[col] + betas[col] + logpk - logll[mb])

            # If FastEmit regularization is enabled, calculate the gradeint of probability of predicting the next label
            # at the current timestep.
            # The formula for this is Equation 9 in https://arxiv.org/abs/2010.11148, multiplied by the log probability
            # of the current step (t, u), normalized by the total log likelihood.
            # Once the gradient has been calculated, scale it by `fastemit_lambda`, as in Equation 10.
            if fastemit_lambda > 0.0 and u < U - 1:
                fastemit_grad = 0.0

                for i in range(num_durations):
                    if t + durations[i] < T:
                        fastemit_grad += fastemit_lambda * math.exp(
                            alphas[col]  # alphas(t, u)
                            + (
                                denom[col] + acts[col * alphabet_size + labels[u]]
                            )  # log prob of token emission
                            + duration_acts[
                                col * num_durations + i
                            ]  # duration log-prob
                            + betas[col + 1 + durations[i] * maxU]  # betas(t, u+1)
                            + logpk  # log Pr(k|t, u)
                            - sigma  # for logit under-normalization
                            - logll[mb]  # total log likelihood for normalization
                        )
            else:
                fastemit_grad = 0.0

            # Update the gradient of act[b, t, u, v] with the gradient from FastEmit regularization
            grad = grad + fastemit_grad

            # grad to last blank transition
            # grad[b, T-1, U-1, v=blank] -= exp(alphas[b, t, u] + logpk - sigma - logll[b] + logp(duration) for all possible non-zero durations.
            if idx == blank_ and u == U - 1:
                for i in range(num_durations):
                    if durations[i] == 0:
                        continue
                    if t == T - durations[i]:
                        grad -= math.exp(
                            alphas[col]
                            + logpk
                            - sigma
                            - logll[mb]
                            + duration_acts[col * num_durations + i]
                        )

            # grad of blank across t < T;
            # grad[b, t<T-1, u, v=blank] -= exp(alphas[b, t, u] + logpk - sigma + logp_duration - logll[b] + betas[b, t + duration, u]) for all non-zero durations
            if idx == blank_:
                for i in range(num_durations):
                    if durations[i] == 0:
                        continue
                    if t < T - durations[i]:
                        grad -= math.exp(
                            alphas[col]
                            + logpk
                            - sigma
                            - logll[mb]
                            + betas[col + maxU * durations[i]]
                            + duration_acts[col * num_durations + i]
                        )

            # grad of correct token across u < U;
            # grad[b, t, u<U-1, v=label[u]] -= exp(alphas[b, t, u] + logpk - sigma + logp_duration - logll[b] + betas[b, t + duration, u + 1]) for all blank durations.
            # Scale the gradient by (1.0 + FastEmit_lambda) in log space, then exponentiate
            if u < U - 1 and idx == labels[u]:
                # exp(log(1 + fastemit_lambda) + ...) is numerically more stable than
                # multiplying (1.0 + fastemit_lambda) with result.
                for i in range(num_durations):
                    if t + durations[i] < T:
                        grad -= math.exp(
                            math.log1p(fastemit_lambda)
                            + alphas[col]
                            + logpk
                            - sigma
                            - logll[mb]
                            + betas[col + 1 + maxU * durations[i]]
                            + duration_acts[col * num_durations + i]
                        )

            # update grads[b, t, u, v] = grad
            label_grads[col * alphabet_size + idx] = grad

            # clamp gradient (if needed)
            if clamp > 0.0:
                g = label_grads[col * alphabet_size + idx]
                g = min(g, clamp)
                g = max(g, -clamp)
                label_grads[col * alphabet_size + idx] = g

            # update internal index through the thread_buffer;
            # until idx < V + 1, such that entire vocabulary has been updated.
            idx += GPU_RNNT_THREAD_SIZE
