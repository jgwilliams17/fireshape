import firedrake as fd
import fireshape as fs
import _ROL as ROL
import math

n = 100
mesh = fd.UnitSquareMesh(n, n)

inner = fs.LaplaceInnerProduct(mesh)
Q = fs.FeControlSpace(mesh, inner)
# # Q = fs.BsplineControlSpace(mesh, inner)
q = fs.ControlVector(Q)
(x, y) = fd.SpatialCoordinate(q.domain())
f = (pow(x-0.5, 2))+pow(y-0.5, 2) - 2.

class LevelsetFunctional(fs.Objective):

    def val(self):
        return fd.assemble(f * fd.dx)

    def derivative_form(self, v):
        return fd.div(f*v) * fd.dx

out = fd.File("domain.pvd")

J = LevelsetFunctional(q, cb=lambda: out.write(q.domain().coordinates))

params_dict = {
    'General': {
        'Secant': { 'Type': 'Limited-Memory BFGS', 'Maximum Storage': 25 } },
    'Step': {
        'Type': 'Line Search',
        'Line Search': { 'Descent Method': { 'Type': 'Quasi-Newton Step' } }
    },
    'Status Test': {
        'Gradient Tolerance': 1e-15, 'Relative Gradient Tolerance': 1e-10,
        'Step Tolerance': 1e-16, 'Relative Step Tolerance': 1e-10,
        'Iteration Limit': 100 }
}

params = ROL.ParameterList(params_dict, "Parameters")
x = q.clone() # we need a seperate iteration vector
problem = ROL.OptimizationProblem(J, x)
solver = ROL.OptimizationSolver(problem, params)
solver.solve()
