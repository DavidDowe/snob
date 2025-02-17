
"""
An estimator for modelling data from a mixture of Gaussians, 
using an objective function based on minimum message length.
"""

__all__ = [
    "GaussianMixture", 
    "kullback_leibler_for_multivariate_normals",
    "responsibility_matrix",
    "split_component", "merge_component", "delete_component", 
] 
import logging
import numpy as np
import scipy
import scipy.optimize as op
from collections import defaultdict

logger = logging.getLogger(__name__)


# OK,. let's see if we can estimate the learning rate \gamma
def _evaluate_gaussian(y, mu, cov):
   N, D = y.shape
   Cinv = np.linalg.inv(cov)
   scale = 1.0/np.sqrt((2*np.pi)**D * np.linalg.det(cov))#
   #Cinv**(-0.5)
   d = y - mu
   return scale * np.exp(-0.5 * np.sum(d.T * np.dot(Cinv, d.T), axis=0))


def _total_parameters(K, D):
    r"""
    Return the total number of model parameters :math:`Q`, if a full 
    covariance matrix structure is assumed.

    .. math:

        Q = \frac{K}{2}\left[D(D+3) + 2\right] - 1


    :param K:
        The number of Gaussian mixtures.

    :param D:
        The dimensionality of the data.

    :returns:
        The total number of model parameters, :math:`Q`.
    """
    return (0.5 * D * (D + 3) * K) + (K - 1)


def _generator_for_approximate_log_likelihood_improvement(K, log_likelihood,
    normalization_factor, *log_likelihoods_of_sequentially_increasing_mixtures):
    
    x = np.arange(K, K + len(log_likelihoods_of_sequentially_increasing_mixtures))
    ll = np.hstack([log_likelihood, *log_likelihoods_of_sequentially_increasing_mixtures])
    y = np.diff(ll) / normalization_factor

    functions = {
        1: lambda x, *p: p[0] / np.exp(x),
        2: lambda x, *p: p[0] / np.exp(p[1] * x),
        3: lambda x, *p: p[0] / np.exp(p[1] * x) + p[2]
    }

    foo = False
    if x.size > 3:
        #x = x[1:]
        #y = y[1:]
        foo = True

    p_opt_ = []
    for d in np.arange(1, 1 + x.size)[::-1]:

        cost_function = functions.get(d, functions[3])
        p0 = np.ones(d)

        try:
            p_opt, p_cov = op.curve_fit(cost_function, x, y, p0=p0)
        except:
            assert d > 1
            continue
        else:
            p_opt_.append(p_opt)
            break

    p_opt = p_opt_.pop(0)
    d = p_opt.size
    cost_function = functions.get(d, functions[3])

    generating_function = lambda target_K: log_likelihood \
        + cost_function(np.arange(1, target_K), *p_opt).sum() * normalization_factor

    if False and foo and x.size > 10:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        ax.scatter(np.arange(ll.size) + 1, ll)
        ax.plot(np.arange(ll.size) + 1, [generating_function(_) for _ in np.arange(ll.size) + 1], c='r')
        raise a

    return generating_function



def _approximate_log_likelihood_improvement(y, mu, cov, weight, 
    log_likelihood, *log_likelihoods_of_sequentially_increasing_mixtures):
    """
    Return a function that will approximate the log-likelihood value for a
    given target number of mixtures.
    """

    # Evaluate the current mixture.
    K = weight.size
    evaluate_f1 = np.sum(weight * np.vstack(
        [_evaluate_gaussian(y, mu[k], cov[k]) for k in range(K)]).T)

    x = np.arange(K, K + len(log_likelihoods_of_sequentially_increasing_mixtures))
    ll = np.hstack([log_likelihood, log_likelihoods_of_sequentially_increasing_mixtures])
    y = np.diff(ll) / evaluate_f1

    d = x.size
    functions = {
        1: lambda x, *p: p[0] / np.exp(x),
        2: lambda x, *p: p[0] / np.exp(p[1] * x),
        3: lambda x, *p: p[0] / np.exp(p[1] * x) + p[2]
    }
    cost_function = functions.get(d, functions[3])
    p0 = np.ones(d)

    p_opt, p_cov = op.curve_fit(cost_function, x, y, p0=p0)
    
    # Now generate the function to estimate the log-likelihood of the K-th
    # mixture.
    generating_function = lambda K: log_likelihood + cost_function(np.arange(1, K-1), *p_opt).sum() * evaluate_f1

    return generating_function



def _approximate_sum_log_weights(target_K):
    r"""
    Return an approximate expectation of the function:

    .. math:

        \sum_{k=1}^{K}\log{w_k}

    Where :math:`K` is the number of mixtures, and :math:`w` is a multinomial
    distribution. The approximating function is:

    .. math:

        \sum_{k=1}^{K}\log{w_k} \approx -K\log{K}

    :param target_K:
        The number of target Gaussian mixtures.
    """

    # TODO: Return a variance on this expectation.
    return -target_K * np.log(target_K)


def _approximate_bound_sum_log_determinate_covariances(target_K, 
    covariance_matrices, covariance_type):
    r"""
    Return an approximate expectation of the function:

    .. math:

        \sum_{k=1}^{K}\log{|\bm{C}_k|}

    Where :math:`C_k` is the covariance matrix of the :math:`K`-th mixture.
    A lower and upper bound is given for the approximation, which is based on
    the current estimates of the covariance matrices. The determinants of
    the target distribution are estimated as:

    .. math:

        |\bm{C}_k| = \frac{k_{current}}{k_{target}}\min\left(|C_k|\right)
    
    and the upper bound as:

    .. math:

        |\bm{C}_k| = \frac{k_{current}}{k_{target}}\max\left(|C_k|\right)

    :param K:
        The target number of Gaussian mixtures.
    
    :param covariance_matrices:
        The current estimate of the covariance matrices.
    
    :param covariance_type:
        The type of structure assumed for the covariance matrix.

    :returns:
        An estimated lower and upper bound on the sum of the logarithm of the
        determinants of a :math:`K` Gaussian mixture.
    """

    # Get the current determinants.
    current_K, D, _ = covariance_matrices.shape
    assert covariance_type == "full" #TODO
    assert target_K > current_K

    current_det = np.linalg.det(covariance_matrices)

    current_det_bounds = np.array([np.min(current_det), np.max(current_det)])
    target_det_bounds = current_K/float(target_K) * current_det_bounds

    return target_K * np.log(target_det_bounds)


def _approximate_message_length_change(target_K, current_weights,
    current_cov, current_log_likelihood, N, initial_ll, normalization_factor, optimized_mixture_lls,
    current_ml=0):
    r"""
    Estimate the change in message length between the current mixture of 
    Gaussians, and a target mixture.
    """

    func = _generator_for_approximate_log_likelihood_improvement(1, initial_ll,
        normalization_factor, *np.hstack([optimized_mixture_lls, current_log_likelihood]))

    current_K, D, _ = current_cov.shape
    delta_K = target_K - current_K
    assert delta_K > 0

    # Calculate everything except the log likelihood.
    delta_I = delta_K * (
            (1 - D/2.0) * np.log(2) \
            + 0.25 * (D * (D+3) + 2) * np.log(N/(2*np.pi))) \
        + 0.5 * (D*(D+3)/2 - 1) * (_approximate_sum_log_weights(target_K) - np.sum(np.log(current_weights))) \
        - np.sum([np.log(current_K + dk) for dk in range(delta_K)]) \
        + 0.5 * np.log(_total_parameters(target_K, D)/float(_total_parameters(current_K, D))) \
        + (D + 2)/2.0 * (
            _approximate_bound_sum_log_determinate_covariances(target_K, current_cov, "full") \
            - np.sum(np.log(np.linalg.det(current_cov)))) \
        - func(target_K) + current_log_likelihood
    # Generate a function.
    print("PREDICTING TARGET {} FROM {}: {}".format(target_K, current_weights.size, delta_I))
    if delta_K == 1:
        assert np.all(delta_I + current_ml > 0)

    return delta_I




class GaussianMixture(object):

    r"""
    Model data from (potentially) many multivariate Gaussian distributions, 
    using minimum message length (MML) as the objective function.

    :param covariance_type: [optional]
        The structure of the covariance matrix for individual components.
        The available options are: `full` for a free covariance matrix, or
        `diag` for a diagonal covariance matrix (default: ``diag``).

    :param covariance_regularization: [optional]
        Regularization strength to add to the diagonal of covariance matrices
        (default: ``0``).

    :param threshold: [optional]
        The relative improvement in message length required before stopping an
        expectation-maximization step (default: ``1e-5``).

    :param max_em_iterations: [optional]
        The maximum number of iterations to run per expectation-maximization
        loop (default: ``10000``).
    """

    parameter_names = ("mean", "covariance", "weight")

    def __init__(self, covariance_type="full", covariance_regularization=0, 
        threshold=1e-5, max_em_iterations=10000, **kwargs):

        available = ("full", "diag", )
        covariance_type = covariance_type.strip().lower()
        if covariance_type not in available:
            raise ValueError("covariance type '{}' is invalid. "\
                             "Must be one of: {}".format(
                                covariance_type, ", ".join(available)))

        if 0 > covariance_regularization:
            raise ValueError(
                "covariance_regularization must be a non-negative float")

        if 0 >= threshold:
            raise ValueError("threshold must be a positive value")

        if 1 > max_em_iterations:
            raise ValueError("max_em_iterations must be a positive integer")

        self._threshold = threshold
        self._max_em_iterations = max_em_iterations
        self._covariance_type = covariance_type
        self._covariance_regularization = covariance_regularization
        return None


    @property
    def covariance_type(self):
        r""" Return the type of covariance stucture assumed. """
        return self._covariance_type


    @property
    def covariance_regularization(self):
        r""" 
        Return the regularization applied to diagonals of covariance matrices.
        """
        return self._covariance_regularization


    @property
    def threshold(self):
        r""" Return the threshold improvement required in message length. """
        return self._threshold


    @property
    def max_em_iterations(self):
        r""" Return the maximum number of expectation-maximization steps. """
        return self._max_em_iterations


    def fast_search(self, y, **kwargs):
        r"""
        Search for the number of components, without running E-M everywhere.
        """

        kwds = dict(
            threshold=self._threshold,
            max_em_iterations=self._max_em_iterations,
            covariance_type=self._covariance_type,
            covariance_regularization=self._covariance_regularization)

        N, D = y.shape

        # Initialize the mixture.
        mean = np.mean(y, axis=0).reshape((1, -1))
        cov = _estimate_covariance_matrix(y, np.ones((1, N)), mean,
            covariance_type, covariance_regularization)

        R, ll, message_length = _expectation(y, mean, cov, weight, **kwds)

        converged = False

        while True:

            # Split components, in order of ones with highest |C|.
            component_indices = np.argsort(np.linalg.det(cov))[::-1]
            split_component_index = component_indices[0]

        
            # Split this mixture.

            split()

            for iteration in range(self.max_em_iterations):

                # Run e-step
                self._expectation()

                # Run m-step
                self._maximization()

                # Calculate P(I_{K+1} - I_{K} < 0).
                # If we should add another component, then run the split
                # again.
                if self._predict_next_mixture_message_length() < best_ml:
                    break

                # If not, then we should check to see if E-M is converged.
                # If it is, then stop. If not, run E-M again.
                if self.threshold >= abs(change):
                    # Here we want to leave the loop and not allow any more
                    # splitting, since it's probably not worthwhile doing.
                    converged = True
                    break


            else:
                print("Warning: max number of EM steps")


            # Did that E-M iteration converge, and we won't split again?
            if converged: break


            # TODO:
            # Need some kind of check to see whether we should have
            # splitted something else.
            break



        # Split the mixture.
        
        # Compute the direction of maximum variance of the single component, and
        # locate two points which are one standard deviation away on either side.
        U, S, V = _svd(cov, self.covariance_type)
        child_mean = mean - np.vstack([+V[0], -V[0]]) * S[0]**0.5

        # Responsibilities are initialized by allocating the data points to the 
        # closest of the two means.
        distance = np.vstack([
            np.sum((y - child_mean[0])**2, axis=1),
            np.sum((y - child_mean[1])**2, axis=1)
        ])
        
        child_responsibility = np.zeros((2, N))
        child_responsibility[np.argmin(distance, axis=0), np.arange(N)] = 1.0

        # Calculate the child covariance matrices.
        child_cov = _estimate_covariance_matrix(y, child_responsibility, child_mean,
            kwargs["covariance_type"], kwargs["covariance_regularization"])

        child_effective_membership = np.sum(child_responsibility, axis=1)    
        child_weight = child_effective_membership.T/child_effective_membership.sum()

        # OK, now run E-step.
        
        raise a


    def search(self, y, **kwargs):
        r"""
        Search for the number of components.
        """

        kwds = dict(
            threshold=self._threshold, 
            max_em_iterations=self._max_em_iterations,
            covariance_type=self.covariance_type, 
            covariance_regularization=self._covariance_regularization)

        # Initialize the mixture.
        mu, cov, weight = _initialize(y, **kwds)
        R, ll, message_length = _expectation(y, mu, cov, weight, **kwds)
                
        # Evaluate the initial function.
        initial_ll = ll.sum()
        normalization_factor = 1.0/_evaluate_gaussian(y, mu[0], cov[0]).sum()


        N, D = y.shape

        # Split the component, run E-M, then approximate the change in the
        # log-likelihood.
        # TODO: We don't even need to run E-M.
        optimized_mixture_lls = []
        actual_sum_log_weights = [np.sum(np.log(weight))]
        actual_sum_log_det_cov = [np.sum(np.log(np.linalg.det(cov)))]
        predicted_sum_log_det_cov = [_approximate_bound_sum_log_determinate_covariances(2,
            cov, "full")]
        while True:

            K = weight.size
            best_perturbations = defaultdict(lambda: [np.inf])

            for k in range(K):
                if K > 1:
                    print("k", k, np.log(np.linalg.det(cov[k])))

                perturbation = split_component(y, mu, cov, weight, R, k, **kwds)
    
                if perturbation[-1] < best_perturbations["split"][-1]:
                    best_perturbations["split"] = [k] + list(perturbation)

            bop, bp = min(best_perturbations.items(), key=lambda x: x[1][-1])
            b_k, b_mu, b_cov, b_weight, b_R, b_meta, b_ml = bp

            print("TOOK {}".format(b_k))

            # Predict say, 5 steps ahead.
            bar = []
            for _ in range(2, 2+20):
                delta = _approximate_message_length_change(K+_, b_weight, b_cov,
                    b_meta["log_likelihood"].sum(), N, initial_ll, normalization_factor,
                    optimized_mixture_lls, b_ml)
                predicted = delta + b_ml
                bar.append(predicted)

            bar = np.array(bar)
            idx = np.argmin(bar.T[1])
            print("PREDICTED WE MOVE TO {}: {} (FROM {})".format(range(2, 2+20)[idx] + K, bar[idx], b_ml))

            # Update our approximations.
            optimized_mixture_lls.append(b_meta["log_likelihood"].sum())

            actual_sum_log_weights.append(np.sum(np.log(b_weight)))
            actual_sum_log_det_cov.append(np.sum(np.log(np.linalg.det(b_cov))))
            predicted_sum_log_det_cov.append(_approximate_bound_sum_log_determinate_covariances(K + 2,
                b_cov, "full"))

            if message_length > b_ml:
                mu, cov, weight, R, meta = (b_mu, b_cov, b_weight, b_R, b_meta)
                message_length = b_ml
                ll = b_meta["log_likelihood"]
            else:
                break


        # weeights could be improved, but they aren;t bad (~10 nats at K=16)
        """
        x = 1 + np.arange(len(actual_sum_log_weights))
        expected = _approximate_sum_log_weights(x)
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        ax.scatter(x, actual_sum_log_weights)
        ax.plot(x, expected)
        """
        
        x = 1 + np.arange(len(predicted_sum_log_det_cov))
        predicted_sum_log_det_cov = np.array(predicted_sum_log_det_cov)

        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        ax.scatter(x, actual_sum_log_det_cov)
        ax.plot(x, predicted_sum_log_det_cov.T[0])
        ax.plot(x, predicted_sum_log_det_cov.T[1])


        raise a



        entries = []
        message_length = b_ml
        ll = -ll

        bar = []

        Q = lambda K: (0.5 * D * (D + 3) * K) + (K - 1)

        while True:

            K = weight.size
            if K >= max_components: break
            best_perturbations = defaultdict(lambda: [np.inf])
            

            foo = weight * np.vstack([
                _evaluate_gaussian(y, mu[k], cov[k]) for k in range(K)]).T


            _, old_ll = responsibility_matrix(y, mu, cov, weight, full_output=True, **kwds)    

            # Split all components.
            for k in range(K):
                p = split_component(y, mu, cov, weight, R, k, **kwds)

                # With each perturbation, calculate gamma.
                p_mu, p_cov, p_weight, p_R, p_meta, p_ml = p
                """
                gamma = K * foo.sum() * (p_ml - b_ml - (
                    np.log(2) + np.log(N)/2.0 - np.log(K) \
                    - 0.5 * (np.sum(np.log(p_weight)) - np.sum(np.log(weight))) \
                    - D * np.log(2)/2.0 + D * (D+3)/4.0 * (np.log(N) + np.sum(np.log(p_weight)) - np.sum(np.log(weight))) \
                    - (D + 2)/2.0 * (np.sum(np.log(np.linalg.det(p_cov))) - np.sum(np.log(np.linalg.det(cov)))) \
                    + 0.25 * (2 * np.log(Q(K+1)/Q(K)) - (D * (D+3) +2) * np.log(2*np.pi))
                    ))

                print("Gamma", K, k, gamma)
                """
                _, new_ll = responsibility_matrix(y, p_mu, p_cov, p_weight,
                    full_output=True, **kwds)

                #gamma2 = K * np.sum(foo.T * (old_ll - new_ll))
                gamma2 = K * np.sum(np.sum(foo, axis=1) * (old_ll - new_ll))
                print("gamma_in_prog", K, k, gamma2)
                """
                gamma2 = (K + 1) * np.sum(foo) * (p_ml - b_ml - (
                    np.log(2) + np.log(N)/2.0 - np.log(K) \
                    - 0.5 * (np.sum(np.log(p_weight)) - np.sum(np.log(weight))) \
                    - D * np.log(2)/2.0 + D * (D+3)/4.0 * (np.log(N) + np.sum(np.log(p_weight)) - np.sum(np.log(weight))) \
                    - (D + 2)/2.0 * (np.sum(np.log(np.linalg.det(p_cov))) - np.sum(np.log(np.linalg.det(cov)))) \
                    + 0.25 * (2 * np.log(Q(K+1)/Q(K)) - (D * (D+3) +2) * np.log(2*np.pi))
                    ))
                """
                #print("MYGAMMA", K, k, gamma2)

                
                # Keep best component
                if p[-1] < best_perturbations["split"][-1]:
                    best_perturbations["split"] = [k, gamma2] + list(p)

            bop, bp = min(best_perturbations.items(), key=lambda x: x[1][-1])
            b_k, b_gamma, b_mu, b_cov, b_weight, b_R, b_meta, b_ml = bp

            # Predict the message length of the K + 1 mixture.
            print("GAMMA", K, b_k, b_gamma)


            ll = -b_meta["log_likelihood"]



            # Predict the message length of the next component.
            print("I predict next mixture will have: {}".format(b_ml + b_gamma))

            iterations += 1
            mu, cov, weight, R = (b_mu, b_cov, b_weight, b_R)

            print(weight.size, message_length)
            entries.append([weight.size, message_length])

            if message_length > b_ml:
                message_length = b_ml
            else:
                #break
                print("CONTINUE ANYWAYS")
                
        entries = np.array(entries)
        bar = np.array(bar)

        raise a


    def fit(self, y, num_components=None, **kwargs):
        r"""
        Minimize the message length of a mixture of Gaussians, 
        using our own search algorithm.

        :param y:
            A :math:`N\times{}D` array of the observations :math:`y`,
            where :math:`N` is the number of observations, and :math:`D` is
            the number of dimensions per observation.

        :returns:
            A tuple containing the optimized parameters ``(mu, cov, weight)``.
        """

        kwds = dict(
            threshold=self._threshold, 
            max_em_iterations=self._max_em_iterations,
            covariance_type=self.covariance_type, 
            covariance_regularization=self._covariance_regularization)

        """

        # Initialize the mixture.
        mu, cov, weight = _initialize(y, **kwds)

        N, D = y.shape
        iterations = 1
        
        R, ll, message_length = _expectation(y, mu, cov, weight, **kwds)
        ll_dl = [(ll, message_length)]

        # Do E-M on this initial component.
        mu, cov, weight, responsibility, meta, dl \
            = _expectation_maximization(y, mu, cov, weight, responsibility=R, 
                **kwds)


        _, log_likelihood = responsibility_matrix(
            y, mu, cov, weight, full_output=True, **kwds)
        nll = -np.sum(log_likelihood)
        I, I_full = _message_length(y, mu, cov, weight, responsibility, nll, full_output=True, **kwds)
        meta.update(message_length=I, message_length_meta=I_full)

        if num_components == 1:
            return (mu, cov, weight, meta)
        
        # Estimate whether we should introduce another mixture.

        if num_components == 2:
            # Split it.
            mu, cov, weight, responsibility, meta, dl \
                = split_component(y, mu, cov, weight, responsibility, 0, **kwds)

            _, log_likelihood = responsibility_matrix(
                y, mu, cov, weight, full_output=True, **kwds)
            nll = -np.sum(log_likelihood)
            I, I_full = _message_length(y, mu, cov, weight, responsibility, nll, full_output=True, **kwds)
            meta.update(message_length=I, message_length_meta=I_full)

            return (mu, cov, weight, meta)
        """

        mu, cov, weight = _initialize(y, **kwds)
        R, ll, message_length = _expectation(y, mu, cov, weight, **kwds)
        ll_dl = [(ll.sum(), message_length)]
        meta = dict(log_likelihood=ll.sum(), message_length=message_length)

        while True:

            K = weight.size
            if K >= num_components: break
            best_perturbations = defaultdict(lambda: [np.inf])
            
            # Split all components.
            for k in range(K):
                p = split_component(y, mu, cov, weight, R, k, **kwds)

                # Keep best split component.
                if p[-1] < best_perturbations["split"][-1]:
                    best_perturbations["split"] = [k] + list(p)


            bop, bp = min(best_perturbations.items(), key=lambda x: x[1][-1])
            b_m, b_mu, b_cov, b_weight, b_R, b_meta, b_ml = bp

            logger.debug("Best operation: {} {}".format(bop, b_ml))

            # Set the new state as the best perturbation.
            message_length = b_ml
            mu, cov, weight, R, meta = (b_mu, b_cov, b_weight, b_R, b_meta)
            meta.update(message_length=b_ml)

        return (mu, cov, weight, meta)
        raise a


        mu3, cov3, weight3, responsibility3, meta3, dl3 \
            = split_component(y, mu2, cov2, weight2, responsibility2, 1, **kwds)

        responsibility_matrix(y, mu2, cov2, weight2, dofail=True, **kwds)


        raise a

        while True:

            M = weight.size
            best_perturbations = defaultdict(lambda: [np.inf])
                
            # Exhaustively split all components.
            for m in range(M):
                p = split_component(y, mu, cov, weight, R, m, **kwds)

                # Keep best split component.
                if p[-1] < best_perturbations["split"][-1]:
                    best_perturbations["split"] = [m] + list(p)
            
            if M > 1:
                # Exhaustively delete all components.
                for m in range(M):
                    p = delete_component(y, mu, cov, weight, R, m, **kwds)

                    # Keep best deleted component.
                    if p[-1] < best_perturbations["delete"][-1]:
                        best_perturbations["delete"] = [m] + list(p)
                
                # Exhaustively merge all components.
                for m in range(M):
                    p = merge_component(y, mu, cov, weight, R, m, **kwds)

                    # Keep best merged component.
                    if p[-1] < best_perturbations["merge"][-1]:
                        best_perturbations["merge"] = [m] + list(p)

            # Get best perturbation.
            bop, bp = min(best_perturbations.items(), key=lambda x: x[1][-1])
            b_m, b_mu, b_cov, b_weight, b_R, b_meta, b_ml = bp

            logger.debug("Best operation: {} {}".format(bop, b_ml))

            if message_length > b_ml:
                # Set the new state as the best perturbation.
                iterations += 1
                message_length = b_ml
                mu, cov, weight, R = (b_mu, b_cov, b_weight, b_R)

            else:
                # None of the perturbations were better than what we had.
                break

        # TODO: a full_output response.
        meta = dict(message_length=message_length)
        return (mu, cov, weight, meta)
        


def responsibility_matrix(y, mu, cov, weight, covariance_type, 
    full_output=False, **kwargs):
    r"""
    Return the responsibility matrix,

    .. math::

        r_{ij} = \frac{w_{j}f\left(x_i;\theta_j\right)}{\sum_{k=1}^{K}{w_k}f\left(x_i;\theta_k\right)}


    where :math:`r_{ij}` denotes the conditional probability of a datum
    :math:`x_i` belonging to the :math:`j`-th component. The effective 
    membership associated with each component is then given by

    .. math::

        n_j = \sum_{i=1}^{N}r_{ij}
        \textrm{and}
        \sum_{j=1}^{M}n_{j} = N


    where something.
    
    :param y:
        The data values, :math:`y`.

    :param mu:
        The mean values of the :math:`K` multivariate normal distributions.

    :param cov:
        The covariance matrices of the :math:`K` multivariate normal
        distributions. The shape of this array will depend on the 
        ``covariance_type``.

    :param weight:
        The current estimates of the relative mixing weight.

    :param full_output: [optional]
        If ``True``, return the responsibility matrix, and the log likelihood,
        which is evaluated for free (default: ``False``).

    :returns:
        The responsibility matrix. If ``full_output=True``, then the
        log likelihood (per observation) will also be returned.
    """

    precision_cholesky = _compute_precision_cholesky(cov, covariance_type)
    weighted_log_prob = np.log(weight) + \
        _estimate_log_gaussian_prob(y, mu, precision_cholesky, covariance_type)

    log_likelihood = scipy.misc.logsumexp(weighted_log_prob, axis=1)
    with np.errstate(under="ignore"):
        log_responsibility = weighted_log_prob - log_likelihood[:, np.newaxis]


    responsibility = np.exp(log_responsibility).T
    
    if kwargs.get("dofail", False):
        raise a

    return (responsibility, log_likelihood) if full_output else responsibility


def kullback_leibler_for_multivariate_normals(mu_a, cov_a, mu_b, cov_b):
    r"""
    Return the Kullback-Leibler distance from one multivariate normal
    distribution with mean :math:`\mu_a` and covariance :math:`\Sigma_a`,
    to another multivariate normal distribution with mean :math:`\mu_b` and 
    covariance matrix :math:`\Sigma_b`. The two distributions are assumed to 
    have the same number of dimensions, such that the Kullback-Leibler 
    distance is

    .. math::

        D_{\mathrm{KL}}\left(\mathcal{N}_{a}||\mathcal{N}_{b}\right) = 
            \frac{1}{2}\left(\mathrm{Tr}\left(\Sigma_{b}^{-1}\Sigma_{a}\right) + \left(\mu_{b}-\mu_{a}\right)^\top\Sigma_{b}^{-1}\left(\mu_{b} - \mu_{a}\right) - k + \ln{\left(\frac{\det{\Sigma_{b}}}{\det{\Sigma_{a}}}\right)}\right)


    where :math:`k` is the number of dimensions and the resulting distance is 
    given in units of nats.

    .. warning::

        It is important to remember that 
        :math:`D_{\mathrm{KL}}\left(\mathcal{N}_{a}||\mathcal{N}_{b}\right) \neq D_{\mathrm{KL}}\left(\mathcal{N}_{b}||\mathcal{N}_{a}\right)`.


    :param mu_a:
        The mean of the first multivariate normal distribution.

    :param cov_a:
        The covariance matrix of the first multivariate normal distribution.

    :param mu_b:
        The mean of the second multivariate normal distribution.

    :param cov_b:
        The covariance matrix of the second multivariate normal distribution.
    
    :returns:
        The Kullback-Leibler distance from distribution :math:`a` to :math:`b`
        in units of nats. Dividing the result by :math:`\log_{e}2` will give
        the distance in units of bits.
    """

    if len(cov_a.shape) == 1:
        cov_a = cov_a * np.eye(cov_a.size)

    if len(cov_b.shape) == 1:
        cov_b = cov_b * np.eye(cov_b.size)

    U, S, V = np.linalg.svd(cov_a)
    Ca_inv = np.dot(np.dot(V.T, np.linalg.inv(np.diag(S))), U.T)

    U, S, V = np.linalg.svd(cov_b)
    Cb_inv = np.dot(np.dot(V.T, np.linalg.inv(np.diag(S))), U.T)

    k = mu_a.size

    offset = mu_b - mu_a
    return 0.5 * np.sum([
          np.trace(np.dot(Ca_inv, cov_b)),
        + np.dot(offset.T, np.dot(Cb_inv, offset)),
        - k,
        + np.log(np.linalg.det(cov_b)/np.linalg.det(cov_a))
    ])


def _parameters_per_mixture(D, covariance_type):
    r"""
    Return the number of parameters per Gaussian component, given the number 
    of observed dimensions and the covariance type.

    :param D:
        The number of dimensions per data point.

    :param covariance_type:
        The structure of the covariance matrix for individual components.
        The available options are: `full` for a free covariance matrix, or
        `diag` for a diagonal covariance matrix.

    :returns:
        The number of parameters required to fully specify the multivariate
        mean and covariance matrix of a :math:`D`-dimensional Gaussian.
    """

    if covariance_type == "full":
        return int(D + D*(D + 1)/2.0)
    elif covariance_type == "diag":
        return 2 * D
    else:
        raise ValueError("unknown covariance type '{}'".format(covariance_type))


def _initialize(y, covariance_type, covariance_regularization, **kwargs):
    r"""
    Return initial estimates of the parameters.

    :param y:
        The data values, :math:`y`.

    :param covariance_type:
        The structure of the covariance matrix for individual components.
        The available options are: `full` for a free covariance matrix, or
        `diag` for a diagonal covariance matrix.

    :param covariance_regularization:
        Regularization strength to add to the diagonal of covariance matrices.


    :returns:
        A three-length tuple containing the initial (multivariate) mean,
        the covariance matrix, and the relative weight.
    """

    # If you *really* know what you're doing, then you can give your own.
    if kwargs.get("__initialize", None) is not None:
        return kwargs.pop("__initialize")

    weight = np.ones((1, 1))
    N, D = y.shape
    mean = np.mean(y, axis=0).reshape((1, -1))

    cov = _estimate_covariance_matrix(y, np.ones((1, N)), mean,
        covariance_type, covariance_regularization)

    return (mean, cov, weight)
    

def _expectation(y, mu, cov, weight, **kwargs):
    r"""
    Perform the expectation step of the expectation-maximization algorithm.

    :param y:
        The data values, :math:`y`.

    :param mu:
        The current best estimates of the (multivariate) means of the :math:`K`
        components.

    :param cov:
        The current best estimates of the covariance matrices of the :math:`K`
        components.

    :param weight:
        The current best estimates of the relative weight of all :math:`K`
        components.

    :param N_component_pars:
        The number of parameters required to specify the mean and covariance
        matrix of a single Gaussian component.

    :returns:
        A three-length tuple containing the responsibility matrix,
        the  log likelihood, and the change in message length.
    """

    responsibility, log_likelihood = responsibility_matrix(
        y, mu, cov, weight, full_output=True, **kwargs)

    nll = -np.sum(log_likelihood)

    I = _message_length(y, mu, cov, weight, responsibility, nll, **kwargs)
    
    return (responsibility, log_likelihood, I)


def log_kappa(D):

    cd = -0.5 * D * np.log(2 * np.pi) + 0.5 * np.log(D * np.pi)
    return -1 + 2 * cd/D



def _message_length(y, mu, cov, weight, responsibility, nll,
    covariance_type, eps=0.10, dofail=False, full_output=False, **kwargs):

    # THIS IS SO BAD

    N, D = y.shape
    M = weight.size

    # I(M) = M\log{2} + constant
    I_m = M # [bits]

    # I(w) = \frac{(M - 1)}{2}\log{N} - \frac{1}{2}\sum_{j=1}^{M}\log{w_j} - (M - 1)!
    I_w = (M - 1) / 2.0 * np.log(N) \
        - 0.5 * np.sum(np.log(weight)) \
        - scipy.special.gammaln(M)

    # TODO: why gammaln(M) ~= log(K-1)! or (K-1)!

    #- np.math.factorial(M - 1) \
    #+ 1
    I_w = I_w/np.log(2) # [bits]


    # \sum_{j=1}^{M}I(\theta_j) = -\sum_{j=1}^{M}\log{h\left(\theta_j\right)}
    #                           + 0.5\sum_{j=1}^{M}\log\det{F\left(\theta_j\right)}

    # \det{F\left(\mu,C\right)} \approx \det{F(\mu)}\dot\det{F(C)}
    # |F(\mu)| = N^{d}|C|^{-1}
    # |F(C)| = N^\frac{d(d + 1)}{2}2^{-d}|C|^{-(d+1)}

    # thus |F(\mu,C)| = N^\frac{d(d+3)}{2}\dot{}2^{-d}\dot{}|C|^{-(d+2)}

    # old:
    """
    log_F_m = 0.5 * D * (D + 3) * np.log(N) \
            - 2 * np.log(D) \
            - (D + 2) * np.nansum(np.log(np.linalg.det(cov)))
    """


    # For multivariate, from Kasaraup:
    """
    log_F_m = 0.5 * D * (D + 3) * np.log(N) # should be Neff? TODO
    log_F_m += -np.sum(log_det_cov)
    log_F_m += -(D * np.log(2) + (D + 1) * np.sum(log_det_cov))    
    """
    if D == 1:
        log_F_m = np.log(2) + (2 * np.log(N)) - 4 * np.log(cov.flatten()[0]**0.5)
        raise UnsureError

    else:
        if covariance_type == "diag":
            cov_ = np.array([_ * np.eye(D) for _ in cov])
        else:
            # full
            cov_ = cov

        log_det_cov = np.log(np.linalg.det(cov_))
    
        log_F_m = 0.5 * D * (D + 3) * np.log(np.sum(responsibility, axis=1)) 
        log_F_m += -log_det_cov
        log_F_m += -(D * np.log(2) + (D + 1) * log_det_cov)

        
    # TODO: No prior on h(theta).. thus -\sum_{j=1}^{M}\log{h\left(\theta_j\right)} = 0

    # TODO: bother about including this? -N * D * np.log(eps)
    

    N_cp = _parameters_per_mixture(D, covariance_type)
    part2 = nll + N_cp/(2*np.log(2))

    AOM = 0.001 # MAGIC
    Il = nll - (D * N * np.log(AOM))
    Il = Il/np.log(2) # [bits]

    """
    if D == 1:log_likelihood

        # R1
        R1 = 10 # MAGIC
        R2 = 2 # MAGIC
        log_prior = D * np.log(R1) # mu
        log_prior += np.log(R2)
        log_prior += np.log(cov.flatten()[0]**0.5)

    
    else:
        R1 = 10
        log_prior = D * np.log(R1) + 0.5 * (D + 1) * log_det_cov
    """
    
    log_prior = 0


    I_t = (log_prior + 0.5 * log_F_m)/np.log(2)
    sum_It = np.sum(I_t)


    num_free_params = (0.5 * D * (D+3) * M) + (M - 1)
    lattice = 0.5 * num_free_params * log_kappa(num_free_params) / np.log(2)


    part1 = I_m + I_w + np.sum(I_t) + lattice
    part2 = Il + (0.5 * num_free_params)/np.log(2)

    I = part1 + part2

    assert I_w >= -0.5 # prevent triggers on underflow

    assert I > 0

    if dofail:
    
        print(I_m, I_w, np.sum(I_t), lattice, Il)
        print(I_t)
        print(part1, part2)

        raise a

    if full_output:
        return (I, dict(I_m=I_m, I_w=I_w, log_F_m=log_F_m, nll=nll, I_l=Il, I_t=I_t,
            lattice=lattice, part1=part1, part2=part2))

    return I


def _compute_precision_cholesky(covariances, covariance_type):
    r"""
    Compute the Cholesky decomposition of the precision of the covariance
    matrices provided.

    :param covariances:
        An array of covariance matrices.

    :param covariance_type:
        The structure of the covariance matrix for individual components.
        The available options are: `full` for a free covariance matrix, or
        `diag` for a diagonal covariance matrix.
    """

    singular_matrix_error = "Failed to do Cholesky decomposition"

    if covariance_type in "full":
        M, D, _ = covariances.shape

        cholesky_precision = np.empty((M, D, D))
        for m, covariance in enumerate(covariances):
            try:
                cholesky_cov = scipy.linalg.cholesky(covariance, lower=True) 
            except scipy.linalg.LinAlgError:
                raise ValueError(singular_matrix_error)


            cholesky_precision[m] = scipy.linalg.solve_triangular(
                cholesky_cov, np.eye(D), lower=True).T

    elif covariance_type in "diag":
        if np.any(np.less_equal(covariances, 0.0)):
            raise ValueError(singular_matrix_error)

        cholesky_precision = covariances**(-0.5)

    else:
        raise NotImplementedError("nope")

    return cholesky_precision



def _estimate_covariance_matrix_full(y, responsibility, mean, 
    covariance_regularization=0):

    N, D = y.shape
    M, N = responsibility.shape

    membership = np.sum(responsibility, axis=1)

    I = np.eye(D)
    cov = np.empty((M, D, D))
    for m, (mu, rm, nm) in enumerate(zip(mean, responsibility, membership)):

        diff = y - mu
        denominator = nm - 1 if nm > 1 else nm

        cov[m] = np.dot(rm * diff.T, diff) / denominator \
               + covariance_regularization * I

    return cov


def _estimate_covariance_matrix(y, responsibility, mean, covariance_type,
    covariance_regularization):

    available = {
        "full": _estimate_covariance_matrix_full,
        "diag": _estimate_covariance_matrix_diag
    }

    try:
        function = available[covariance_type]

    except KeyError:
        raise ValueError("unknown covariance type")

    return function(y, responsibility, mean, covariance_regularization)

def _estimate_covariance_matrix_diag(y, responsibility, mean, 
    covariance_regularization=0):

    N, D = y.shape
    M, N = responsibility.shape

    denominator = np.sum(responsibility, axis=1)
    denominator[denominator > 1] = denominator[denominator > 1] - 1

    membership = np.sum(responsibility, axis=1)

    I = np.eye(D)
    cov = np.empty((M, D))
    for m, (mu, rm, nm) in enumerate(zip(mean, responsibility, membership)):

        diff = y - mu
        denominator = nm - 1 if nm > 1 else nm

        cov[m] = np.dot(rm, diff**2) / denominator + covariance_regularization

    return cov

    


def _compute_log_det_cholesky(matrix_chol, covariance_type, n_features):
    """Compute the log-det of the cholesky decomposition of matrices.
    Parameters
    ----------
    matrix_chol : array-like,
        Cholesky decompositions of the matrices.
        'full' : shape of (n_components, n_features, n_features)
        'tied' : shape of (n_features, n_features)
        'diag' : shape of (n_components, n_features)
        'spherical' : shape of (n_components,)
    covariance_type : {'full', 'tied', 'diag', 'spherical'}
    n_features : int
        Number of features.
    Returns
    -------
    log_det_precision_chol : array-like, shape (n_components,)
        The determinant of the precision matrix for each component.
    """
    if covariance_type == 'full':
        n_components, _, _ = matrix_chol.shape
        log_det_chol = (np.sum(np.log(
            matrix_chol.reshape(
                n_components, -1)[:, ::n_features + 1]), 1))

    elif covariance_type == 'tied':
        log_det_chol = (np.sum(np.log(np.diag(matrix_chol))))

    elif covariance_type == 'diag':
        log_det_chol = (np.sum(np.log(matrix_chol), axis=1))

    else:
        log_det_chol = n_features * (np.log(matrix_chol))

    return log_det_chol


def _estimate_log_gaussian_prob(X, means, precision_cholesky, covariance_type):
    n_samples, n_features = X.shape
    n_components, _ = means.shape
    # det(precision_chol) is half of det(precision)
    log_det = _compute_log_det_cholesky(
        precision_cholesky, covariance_type, n_features)

    if covariance_type in 'full':
        log_prob = np.empty((n_samples, n_components))
        for k, (mu, prec_chol) in enumerate(zip(means, precision_cholesky)):
            y = np.dot(X, prec_chol) - np.dot(mu, prec_chol)
            log_prob[:, k] = np.sum(np.square(y), axis=1)

    elif covariance_type in 'diag':
        precisions = precision_cholesky**2
        log_prob = (np.sum((means ** 2 * precisions), 1) - 2.0 * np.dot(X, (means * precisions).T) + np.dot(X**2, precisions.T))

    return -0.5 * (n_features * np.log(2 * np.pi) + log_prob) + log_det


def _maximization(y, mu, cov, weight, responsibility, parent_responsibility=1,
    **kwargs):
    r"""
    Perform the maximization step of the expectation-maximization algorithm
    on all components.

    :param y:
        The data values, :math:`y`.

    :param mu:
        The current estimates of the Gaussian mean values.

    :param cov:
        The current estimates of the Gaussian covariance matrices.

    :param weight:
        The current best estimates of the relative weight of all :math:`K`
        components.

    :param responsibility:
        The responsibility matrix for all :math:`N` observations being
        partially assigned to each :math:`K` component.
    
    :param parent_responsibility: [optional]
        An array of length :math:`N` giving the parent component 
        responsibilities (default: ``1``). Only useful if the maximization
        step is to be performed on sub-mixtures with parent responsibilities.

    :returns:
        A three length tuple containing the updated multivariate mean values,
        the updated covariance matrices, and the updated mixture weights. 
    """

    M = weight.size 
    N, D = y.shape
    
    # Update the weights.
    effective_membership = np.sum(responsibility, axis=1)
    new_weight = (effective_membership + 0.5)/(N + M/2.0)

    w_responsibility = parent_responsibility * responsibility
    w_effective_membership = np.sum(w_responsibility, axis=1)

    new_mu = np.zeros_like(mu)
    new_cov = np.zeros_like(cov)
    for m in range(M):
        new_mu[m] = np.sum(w_responsibility[m] * y.T, axis=1) \
                  / w_effective_membership[m]

    new_cov = _estimate_covariance_matrix(y, responsibility, new_mu,
        kwargs["covariance_type"], kwargs["covariance_regularization"])

    state = (new_mu, new_cov, new_weight)

    assert np.all(np.isfinite(new_mu))
    assert np.all(np.isfinite(new_cov))
    assert np.all(np.isfinite(new_weight))

    return state 




def _expectation_maximization(y, mu, cov, weight, responsibility=None, **kwargs):
    r"""
    Run the expectation-maximization algorithm on the current set of
    multivariate Gaussian mixtures.

    :param y:
        A :math:`N\times{}D` array of the observations :math:`y`,
        where :math:`N` is the number of observations, and :math:`D` is the
        number of dimensions per observation.

    :param mu:
        The current estimates of the Gaussian mean values.

    :param cov:
        The current estimates of the Gaussian covariance matrices.

    :param weight:
        The current estimates of the relative mixing weight.

    :param responsibility: [optional]
        The responsibility matrix for all :math:`N` observations being
        partially assigned to each :math:`K` component. If ``None`` is given
        then the responsibility matrix will be calculated in the first
        expectation step.

    :param covariance_type: [optional]
        The structure of the covariance matrix for individual components.
        The available options are: `free` for a free covariance matrix,
        `diag` for a diagonal covariance matrix, `tied` for a common covariance
        matrix for all components, `tied_diag` for a common diagonal
        covariance matrix for all components (default: ``free``).

    :param threshold: [optional]
        The relative improvement in log likelihood required before stopping
        an expectation-maximization step (default: ``1e-5``).

    :param max_em_iterations: [optional]
        The maximum number of iterations to run per expectation-maximization
        loop (default: ``10000``).

    :returns:
        A six length tuple containing: the updated multivariate mean values,
        the updated covariance matrices, the updated mixture weights, the
        updated responsibility matrix, a metadata dictionary, and the change
        in message length.
    """   

    M = weight.size
    N, D = y.shape
    
    # Calculate log-likelihood and initial expectation step.
    _init_responsibility, ll, dl = _expectation(y, mu, cov, weight, **kwargs)

    if responsibility is None:
        responsibility = _init_responsibility

    iterations = 1
    ll_dl = [(ll.sum(), dl)]

    while True:

        # Perform the maximization step.
        mu, cov, weight \
            = _maximization(y, mu, cov, weight, responsibility, **kwargs)

        # Run the expectation step.
        responsibility, ll, dl \
            = _expectation(y, mu, cov, weight, **kwargs)

        # Check for convergence.
        lls = np.sum(ll)
        prev_ll, prev_dl = ll_dl[-1]
        relative_delta_message_length = np.abs((lls - prev_ll)/prev_ll)
        ll_dl.append([lls, dl])
        iterations += 1

        print(iterations, lls)

        assert np.isfinite(relative_delta_message_length)

        if relative_delta_message_length <= kwargs["threshold"] \
        or iterations >= kwargs["max_em_iterations"]:
            break

    print("RAN {} E-M steps".format(iterations))

    meta = dict(warnflag=iterations >= kwargs["max_em_iterations"], log_likelihood=ll)
    if meta["warnflag"]:
        logger.warn("Maximum number of E-M iterations reached ({}) {}".format(
            kwargs["max_em_iterations"], kwargs.get("_warn_context", "")))

    return (mu, cov, weight, responsibility, meta, dl)


def _svd(covariance, covariance_type):

    if covariance_type == "full":
        return np.linalg.svd(covariance)

    elif covariance_type == "diag":
        return np.linalg.svd(covariance * np.eye(covariance.size))

    else:
        raise ValueError("unknown covariance type")


def split_component(y, mu, cov, weight, responsibility, index, **kwargs):
    r"""
    Split a component from the current mixture and determine the new optimal
    state.

    :param y:
        A :math:`N\times{}D` array of the observations :math:`y`,
        where :math:`N` is the number of observations, and :math:`D` is the
        number of dimensions per observation.

    :param mu:
        The current estimates of the Gaussian mean values.

    :param cov:
        The current estimates of the Gaussian covariance matrices.

    :param weight:
        The current estimates of the relative mixing weight.

    :param responsibility:
        The responsibility matrix for all :math:`N` observations being
        partially assigned to each :math:`K` component.

    :param index:
        The index of the component to be split.

    :param covariance_type: [optional]
        The structure of the covariance matrix for individual components.
        The available options are: `free` for a free covariance matrix,
        `diag` for a diagonal covariance matrix, `tied` for a common covariance
        matrix for all components, `tied_diag` for a common diagonal
        covariance matrix for all components (default: ``free``).

    :returns:
        A six length tuple containing: the updated multivariate mean values,
        the updated covariance matrices, the updated mixture weights, the
        updated responsibility matrix, a metadata dictionary, and the change
        in message length.
    """

    # TODO: Current implementation only allows for a component to be split
    #       into two sub-components

    logger.debug("Splitting component {} of {}".format(index, weight.size))

    M = weight.size
    N, D = y.shape
    
    # Compute the direction of maximum variance of the parent component, and
    # locate two points which are one standard deviation away on either side.
    U, S, V = _svd(cov[index], kwargs["covariance_type"])

    child_mu = mu[index] - np.vstack([+V[0], -V[0]]) * S[0]**0.5

    assert np.all(np.isfinite(child_mu))

    # Responsibilities are initialized by allocating the data points to the 
    # closest of the two means.
    distance = np.vstack([
        np.sum((y - child_mu[0])**2, axis=1),
        np.sum((y - child_mu[1])**2, axis=1)
    ])
    
    child_responsibility = np.zeros((2, N))
    child_responsibility[np.argmin(distance, axis=0), np.arange(N)] = 1.0

    # Calculate the child covariance matrices.
    child_cov = _estimate_covariance_matrix(y, child_responsibility, child_mu,
        kwargs["covariance_type"], kwargs["covariance_regularization"])

    child_effective_membership = np.sum(child_responsibility, axis=1)    
    child_weight = child_effective_membership.T/child_effective_membership.sum()

    # We will need these later.
    parent_weight = weight[index]
    parent_responsibility = responsibility[index]

    """
    # Run expectation-maximization on the child mixtures.
    child_mu, child_cov, child_weight, child_responsibility, meta, dl = \
        _expectation_maximization(y, child_mu, child_cov, child_weight, 
            responsibility=child_responsibility, 
            parent_responsibility=parent_responsibility,
            covariance_type=covariance_type, **kwargs)
    """

    # After the chld mixture is locally optimized, we need to integrate it
    # with the untouched M - 1 components to result in a M + 1 component
    # mixture M'.

    # An E-M is finally carried out on the combined M + 1 components to
    # estimate the parameters of M' and result in an optimized 
    # (M + 1)-component mixture.

    # Update the component weights.
    # Note that the child A mixture will remain in index `index`, and the
    # child B mixture will be appended to the end.

    if M > 1:

        # Integrate the M + 1 components and run expectation-maximization
        weight = np.hstack([weight, [parent_weight * child_weight[1]]])
        weight[index] = parent_weight * child_weight[0]

        responsibility = np.vstack([responsibility, 
            [parent_responsibility * child_responsibility[1]]])
        responsibility[index] = parent_responsibility * child_responsibility[0]
        
        mu = np.vstack([mu, [child_mu[1]]])
        mu[index] = child_mu[0]

        cov = np.vstack([cov, [child_cov[1]]])
        cov[index] = child_cov[0]

        mu, cov, weight, responsibility, meta, ml = _expectation_maximization(
            y, mu, cov, weight, responsibility, **kwargs)


    else:
        # Simple case where we don't have to re-run E-M because there was only
        # one component to split.
        child_mu, child_cov, child_weight, child_responsibility, meta, ml = \
            _expectation_maximization(y, child_mu, child_cov, child_weight, 
            responsibility=child_responsibility, 
            parent_responsibility=parent_responsibility, **kwargs)

        mu, cov, weight, responsibility \
            = (child_mu, child_cov, child_weight, child_responsibility)

    return (mu, cov, weight, responsibility, meta, ml)


def delete_component(y, mu, cov, weight, responsibility, index, **kwargs):
    r"""
    Delete a component from the mixture, and return the new optimal state.

    :param y:
        A :math:`N\times{}D` array of the observations :math:`y`,
        where :math:`N` is the number of observations, and :math:`D` is the
        number of dimensions per observation.

    :param mu:
        The current estimates of the Gaussian mean values.

    :param cov:
        The current estimates of the Gaussian covariance matrices.

    :param weight:
        The current estimates of the relative mixing weight.

    :param responsibility:
        The responsibility matrix for all :math:`N` observations being
        partially assigned to each :math:`K` component.

    :param index:
        The index of the component to be deleted.

    :param covariance_type: [optional]
        The structure of the covariance matrix for individual components.
        The available options are: `free` for a free covariance matrix,
        `diag` for a diagonal covariance matrix, `tied` for a common covariance
        matrix for all components, `tied_diag` for a common diagonal
        covariance matrix for all components (default: ``free``).

    :returns:
        A six length tuple containing: the updated multivariate mean values,
        the updated covariance matrices, the updated mixture weights, the
        updated responsibility matrix, a metadata dictionary, and the change
        in message length.
    """

    logger.debug("Deleting component {} of {}".format(index, weight.size))

    # Create new component weights.
    parent_weight = weight[index]
    parent_responsibility = responsibility[index]
    
    # Eq. 54-55
    new_weight = np.clip(
        np.delete(weight, index, axis=0)/(1-parent_weight),
        0, 1)
    
    # Calculate the new responsibility safely.
    new_responsibility = np.clip(
        np.delete(responsibility, index, axis=0) / (1 - parent_responsibility),
        0, 1)
    new_responsibility[~np.isfinite(new_responsibility)] = 0.0

    assert np.all(np.isfinite(new_responsibility))
    assert np.all(np.isfinite(new_weight))

    new_mu = np.delete(mu, index, axis=0)
    new_cov = np.delete(cov, index, axis=0)

    # Run expectation-maximizaton on the perturbed mixtures. 
    return _expectation_maximization(y, new_mu, new_cov, new_weight, 
        new_responsibility, **kwargs)


def merge_component(y, mu, cov, weight, responsibility, index, **kwargs):
    r"""
    Merge a component from the mixture with its "closest" component, as
    judged by the Kullback-Leibler distance.

    :param y:
        A :math:`N\times{}D` array of the observations :math:`y`,
        where :math:`N` is the number of observations, and :math:`D` is the
        number of dimensions per observation.

    :param mu:
        The current estimates of the Gaussian mean values.

    :param cov:
        The current estimates of the Gaussian covariance matrices.

    :param weight:
        The current estimates of the relative mixing weight.

    :param responsibility:
        The responsibility matrix for all :math:`N` observations being
        partially assigned to each :math:`K` component.

    :param index:
        The index of the component to be deleted.

    :param covariance_type: [optional]
        The structure of the covariance matrix for individual components.
        The available options are: `free` for a free covariance matrix,
        `diag` for a diagonal covariance matrix, `tied` for a common covariance
        matrix for all components, `tied_diag` for a common diagonal
        covariance matrix for all components (default: ``free``).

    :returns:
        A six length tuple containing: the updated multivariate mean values,
        the updated covariance matrices, the updated mixture weights, the
        updated responsibility matrix, a metadata dictionary, and the change
        in message length.
    """

    # Calculate the Kullback-Leibler distance to the other distributions.
    D_kl = np.inf * np.ones(weight.size)
    for m in range(weight.size):
        if m == index: continue
        D_kl[m] = kullback_leibler_for_multivariate_normals(
            mu[index], cov[index], mu[m], cov[m])

    a_index, b_index = (index, np.nanargmin(D_kl))

    logger.debug("Merging component {} (of {}) with {}".format(
        a_index, weight.size, b_index))


    # Initialize.
    weight_k = np.sum(weight[[a_index, b_index]])
    responsibility_k = np.sum(responsibility[[a_index, b_index]], axis=0)
    effective_membership_k = np.sum(responsibility_k)

    mu_k = np.sum(responsibility_k * y.T, axis=1) / effective_membership_k
    cov_k = _estimate_covariance_matrix(
        y, np.atleast_2d(responsibility_k), np.atleast_2d(mu_k), 
        kwargs["covariance_type"], kwargs["covariance_regularization"])

    # Delete the b-th component.
    del_index = np.max([a_index, b_index])
    keep_index = np.min([a_index, b_index])

    new_mu = np.delete(mu, del_index, axis=0)
    new_cov = np.delete(cov, del_index, axis=0)
    new_weight = np.delete(weight, del_index, axis=0)
    new_responsibility = np.delete(responsibility, del_index, axis=0)

    new_mu[keep_index] = mu_k
    new_cov[keep_index] = cov_k
    new_weight[keep_index] = weight_k
    new_responsibility[keep_index] = responsibility_k

    # Calculate log-likelihood.
    return _expectation_maximization(y, new_mu, new_cov, new_weight,
        responsibility=new_responsibility,  **kwargs)