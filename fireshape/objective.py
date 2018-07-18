import ROL
import firedrake as fd
from .control import ControlSpace
from .pde_constraint import PdeConstraint


class Objective(ROL.Objective):

    def __init__(self, Q: ControlSpace, cb=None, scale: float = 1.0,
                 quadrature_degree: int = None):

        """
        Inputs: Q: ControlSpace
                cb: method to store current shape iterate at self.udpate
                scale: scaling factor that multiplies shape
                       functional and directional derivative
                quadrature_degree: quadrature degree to use. If None, then
                ufl will guesstimate the degree
        """
        super().__init__()
        self.Q = Q  # ControlSpace
        self.V_r = Q.V_r  # fd.VectorFunctionSpace on reference mesh
        self.V_m = Q.V_m  # clone of V_r of physical mesh
        self.cb = cb
        self.scale = scale
        self.deriv_r = fd.Function(self.V_r)
        if quadrature_degree is not None:
            self.params = {"quadrature_degree": quadrature_degree}
        else:
            self.params = None

    def value_form(self):
        """UFL formula of misfit functional."""
        raise NotImplementedError

    def value(self, x, tol):
        """Evaluate misfit functional. Function signature imposed by ROL."""
        return self.scale * fd.assemble(self.value_form(),
                                        form_compiler_parameters=self.params)

    def derivative_form(self, v):
        """
        UFL formula of partial shape directional derivative
        """
        raise NotImplementedError

    def derivative(self, out):
        """
        Derivative of the objective (element in dual of space)
        """
        raise NotImplementedError

    def gradient(self, g, x, tol):
        """
        Compute Riesz representative of shape directional derivative.
        Function signature imposed by ROL.
        """

        self.derivative(g)
        g.apply_riesz_map()

    def hessVec(self, hv, v, x, tol):
        raise NotImplementedError

    def update(self, x, flag, iteration):
        """Update physical domain and possibly store current iterate."""
        self.Q.update_domain(x)
        if iteration >= 0 and self.cb is not None:
            self.cb()

    def __add__(self, other):
        if isinstance(other, Objective):
            return ObjectiveSum(self, other)

    def __mul__(self, alpha):
        return ScaledObjective(self, alpha)

    def __rmul__(self, alpha):
        return ScaledObjective(self, alpha)


class ShapeObjective(Objective):
    """Abstract class of shape functionals."""
    def __init__(self, *args, **kwargs):
        """
        Construct a shape functional.

        Preallocate vectors for directional derivatives with respect to
        perturbations in self.V_m, for their clone on self.V_r, and for
        the directional derivative wrt perturbations in ControlSpace (so
        that they are not created every time the derivative is evaluated).
        Note that self.deriv_r is updated whenever self.deriv_m is.
        """
        super().__init__(*args, **kwargs)

        self.deriv_m = fd.Function(self.V_m, val=self.deriv_r)

    def derivative(self, out):
        """
        Assemble partial directional derivative wrt ControlSpace perturbations.

        First, assemble directional derivative (wrt FEspace V_m) and
        store it in self.deriv_m. This automatically updates self.deriv_r,
        which is then converted to the directional derivative wrt
        ControSpace perturbations restrict.
        """
        v = fd.TestFunction(self.V_m)
        fd.assemble(self.derivative_form(v), tensor=self.deriv_m,
                    form_compiler_parameters=self.params)
        out.from_first_derivative(self.deriv_r)
        out.scale(self.scale)
        # return self.deriv_control

    def hessVec(self, hv, v, x, tol):
        Tv = fd.Function(self.V_r)
        v.controlspace.interpolate(v, Tv)
        Tvm = fd.Function(self.V_m, val=Tv)
        test = fd.TestFunction(self.V_m)
        hessVecDualFe = fd.assemble(self.second_derivative_form(Tvm, test), tensor=self.deriv_m)
        v.controlspace.restrict(hessVecDualFe, hv)
        hv.apply_riesz_map()
        hv.scale(self.scale)


class DeformationObjective(Objective):
    """
    Abstract class for functionals that depend on the deformation of the mesh.
    These are different from shape functionals, as they are entirely defined on
    the reference mesh. Examples are regularizing functionals like
    J(f) = int |nabla(f)| dx.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def derivative(self, out):
        """
        Assemble partial directional derivative wrt ControlSpace perturbations.
        """
        v = fd.TestFunction(self.V_r)
        fd.assemble(self.derivative_form(v), tensor=self.deriv_r,
                    form_compiler_parameters=self.params)
        out.from_first_derivative(self.deriv_r)
        out.scale(self.scale)


class ControlObjective(Objective):

    """
    Similar to DeformationObjective, but in the case of a
    FeMultigridConstrolSpace might want to formulate functionals
    in term of the deformation defined on the coarse grid,
    and not in terms of the prolonged deformation.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        (self.V_control, I) = self.Q.get_space_for_inner()
        assert I is None
        self.deriv_r = fd.Function(self.V_control)

    def derivative(self, out):
        """
        Assemble partial directional derivative wrt ControlSpace perturbations.
        """
        v = fd.TestFunction(self.V_control)
        fd.assemble(self.derivative_form(v), tensor=self.deriv_r,
                    form_compiler_parameters=self.params)
        out.fun.assign(self.deriv_r)
        out.scale(self.scale)

    def update(self, x, flag, iteration):
        self.f.assign(x.fun)
        super().update(x, flag, iteration)


class ReducedObjective(ShapeObjective):
    """Abstract class of reduced shape functionals."""
    def __init__(self, J: Objective, e: PdeConstraint):
        if not isinstance(J, ShapeObjective):
            msg = "PDE constraints are currently only supported"
            + " for shape objectives."
            raise NotImplementedError(msg)

        super().__init__(J.Q, J.cb)
        self.J = J
        self.e = e

    def value(self, x, tol):
        """
        Evaluate reduced objective.
        Function signature imposed by ROL.
        """
        return self.J.value(x, tol)

    def derivative_form(self, v):
        """
        The derivative of the reduced objective is given by the derivative of
        the Lagrangian.
        """
        return (self.J.scale * self.J.derivative_form(v)
                + self.e.derivative_form(v))

    def hessVec(self, hv, v, x, tol):
        Tv = fd.Function(self.V_r)
        v.controlspace.interpolate(v, Tv)
        Tvm = fd.Function(self.V_m, val=Tv)
        test = fd.TestFunction(self.V_m)

        u = self.e.solution
        u_adj = self.e.solution_adj
        J = self.J.value_form()
        F = self.e.F
        X = fd.SpatialCoordinate(u.ufl_domain())
        s = Tvm
        
        L = J + fd.replace(F, {F.arguments()[0]: u_adj})
        params = self.e.params

        y_s = fd.Function(self.e.V)
        # follow p 65 of Hinze, Pinnau, Ulbrich, Ulbrich
        # Step 1:
        fd.solve(
            fd.assemble(fd.derivative(F, u), mat_type="aij"),
            y_s,
            fd.assemble(fd.derivative(-F, X, s)),
            solver_parameters=params,
            bcs=fd.homogenize(self.e.bcs)
        )
        # Step 2:
        # from IPython import embed; embed()
        Lyy_y_s = fd.assemble(fd.derivative(fd.derivative(L, u), u, y_s))
        Lyu_s = fd.assemble(fd.derivative(fd.derivative(L, u), X, s))

        h1 = Lyy_y_s
        h1 += Lyu_s

        Luy_y_s = fd.assemble(fd.derivative(fd.derivative(L, X), u, y_s))
        Luu_s = fd.assemble(fd.derivative(fd.derivative(L, X), X, s))
        h2 = Luy_y_s
        h2 += Luu_s

        h3_temp = fd.Function(self.e.V)
        # Step 3:
        bil_form = fd.adjoint(fd.derivative(F, u))
        fd.solve(fd.assemble(bil_form, mat_type="aij"), h3_temp, h1, solver_parameters=params,
            bcs=fd.homogenize(self.e.bcs)
                 )
        F_h3_temp = fd.replace(F, {F.arguments()[0]: h3_temp})
        h3 = fd.assemble(fd.derivative(-F_h3_temp, X))
        # from IPython import embed; embed()
        res = h2
        res += h3
        v.controlspace.restrict(res, hv)
        hv.apply_riesz_map()
        hv.scale(self.scale)

    def update(self, x, flag, iteration):
        """Update domain and solution to state and adjoint equation."""
        self.Q.update_domain(x)
        try:
            self.e.solve()
            self.e.solve_adjoint(self.J.scale * self.J.value_form())
        except fd.ConvergenceError:
            if self.cb is not None:
                self.cb()
            raise
        if iteration >= 0 and self.cb is not None:
            self.cb()


class ObjectiveSum(Objective):

    def __init__(self, a, b):
        super().__init__(a.Q)
        self.a = a
        self.b = b

    def value(self, x, tol):
        return self.a.value(x, tol) + self.b.value(x, tol)

    def value_form(self):
        return self.a.value_form() + self.b.value_form()

    def derivative(self, out):
        temp = out.clone()
        self.a.derivative(out)
        self.b.derivative(temp)
        out.plus(temp)

    def derivative_form(self, v):
        return self.a.derivative_form(v) + self.b.derivative_form(v)

    def update(self, *args):
        self.a.update(*args)
        self.b.update(*args)

    def hessVec(self, hv, v, x, tol):
        temp = hv.clone()
        self.a.hessVec(hv, v, x, tol)
        self.b.hessVec(temp, v, x, tol)
        hv.plus(temp)


class ScaledObjective(Objective):

    def __init__(self, J, alpha):
        super().__init__(J.Q)
        self.J = J
        self.alpha = alpha

    def value(self, *args):
        return self.alpha * self.J.value(*args)

    # def value_form(self):
    #     return self.alpha * self.J.value_form()

    def derivative(self, out):
        self.J.derivative(out)
        out.scale(self.alpha)

    # def derivative_form(self, v):
    #     return self.alpha * self.derivative_form(v)

    def update(self, *args):
        self.J.update(*args)

    def hessVec(self, hv, v, x, tol):
        self.J.hessVec(hv, v, x, tol)
        hv.scale(self.alpha)
