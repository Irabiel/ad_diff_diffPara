# Copyright (c) 2016-2018, The University of Texas at Austin 
# & University of California--Merced.
# Copyright (c) 2019-2020, The University of Texas at Austin 
# University of California--Merced, Washington University in St. Louis.
#
# All Rights reserved.
# See file COPYRIGHT for details.
#
# This file is part of the hIPPYlib library. For more information and source code
# availability see https://hippylib.github.io.
#
# hIPPYlib is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License (as published by the Free
# Software Foundation) version 2.0 dated June 1991.

import dolfin as dl
import ufl
import numpy as np
import matplotlib.pyplot as plt
import argparse

import sys
import os

sys.path.append(os.environ.get('HIPPYLIB_BASE_DIR', "../../"))
from hippylib import *


class SpaceTimePointwiseStateObservation(Misfit):

    def __init__(self, Vh,
                 observation_times,
                 targets,
                 d=None,
                 noise_variance=None):

        self.Vh = Vh
        self.observation_times = observation_times

        self.B = assemblePointwiseObservation(self.Vh, targets)
        self.ntargets = targets

        if d is None:
            self.d = TimeDependentVector(observation_times)
            self.d.initialize(self.B, 0)
        else:
            self.d = d

        self.noise_variance = noise_variance

        ## TEMP Vars
        self.u_snapshot = dl.Vector()
        self.Bu_snapshot = dl.Vector()
        self.d_snapshot = dl.Vector()
        self.B.init_vector(self.u_snapshot, 1)
        self.B.init_vector(self.Bu_snapshot, 0)
        self.B.init_vector(self.d_snapshot, 0)

    def observe(self, x, obs):
        obs.zero()

        for t in self.observation_times:
            x[STATE].retrieve(self.u_snapshot, t)
            self.B.mult(self.u_snapshot, self.Bu_snapshot)
            obs.store(self.Bu_snapshot, t)

    def cost(self, x):
        c = 0
        for t in self.observation_times:
            x[STATE].retrieve(self.u_snapshot, t)
            self.B.mult(self.u_snapshot, self.Bu_snapshot)
            self.d.retrieve(self.d_snapshot, t)
            self.Bu_snapshot.axpy(-1., self.d_snapshot)
            c += self.Bu_snapshot.inner(self.Bu_snapshot)

        return c / (2. * self.noise_variance)

    def grad(self, i, x, out):
        out.zero()
        if i == STATE:
            for t in self.observation_times:
                x[STATE].retrieve(self.u_snapshot, t)
                self.B.mult(self.u_snapshot, self.Bu_snapshot)
                self.d.retrieve(self.d_snapshot, t)
                self.Bu_snapshot.axpy(-1., self.d_snapshot)
                self.Bu_snapshot *= 1. / self.noise_variance
                self.B.transpmult(self.Bu_snapshot, self.u_snapshot)
                out.store(self.u_snapshot, t)
        else:
            pass

    def setLinearizationPoint(self, x, gauss_newton_approx=False):
        pass

    def apply_ij(self, i, j, direction, out):
        out.zero()
        if i == STATE and j == STATE:
            for t in self.observation_times:
                direction.retrieve(self.u_snapshot, t)
                self.B.mult(self.u_snapshot, self.Bu_snapshot)
                self.Bu_snapshot *= 1. / self.noise_variance
                self.B.transpmult(self.Bu_snapshot, self.u_snapshot)
                out.store(self.u_snapshot, t)
        else:
            pass


class TimeDependentAD:
    def __init__(self, mesh, Vh, prior, misfit, simulation_times, wind_velocity, gls_stab, IC):
        self.mesh = mesh
        self.Vh = Vh
        self.prior = prior
        self.misfit = misfit
        self.ic = IC
        # Assume constant timestepping
        self.simulation_times = simulation_times
        dt = simulation_times[1] - simulation_times[0]
        self.dt = dt

        u = dl.TrialFunction(Vh[STATE])
        v = dl.TestFunction(Vh[STATE])

        def pde_varf(u, m, p):
            return ufl.exp(m) * ufl.inner(dl.grad(u), dl.grad(p)) * ufl.dx + ufl.inner(wind_velocity,
                                                                                       dl.grad(u)) * p * ufl.dx

        self.pde_varf = pde_varf

        kappa = dl.Constant(.001)
        dt_expr = dl.Constant(dt)

        r_trial = u + dt_expr * (-ufl.div(kappa * ufl.grad(u)) + ufl.inner(wind_velocity, ufl.grad(u)))
        r_test = v + dt_expr * (-ufl.div(kappa * ufl.grad(v)) + ufl.inner(wind_velocity, ufl.grad(v)))

        h = dl.CellDiameter(mesh)
        vnorm = ufl.sqrt(ufl.inner(wind_velocity, wind_velocity))
        if gls_stab:
            tau = ufl.min_value((h * h) / (dl.Constant(2.) * kappa), h / vnorm)
        else:
            tau = dl.Constant(0.)

        self.stab = dl.assemble( tau*ufl.inner(r_trial, r_test)*ufl.dx)

        self.M = dl.assemble(ufl.inner(u, v) * ufl.dx)
        self.M_stab = dl.assemble(ufl.inner(u, v + tau * r_test) * ufl.dx)
        self.Mt_stab = dl.assemble(ufl.inner(u + tau * r_trial, v) * ufl.dx)

        # Part of model public API
        self.gauss_newton_approx = True
        self.x = [None, None, None]

    def generate_vector(self, component="ALL"):
        if component == "ALL":
            u = TimeDependentVector(self.simulation_times)
            u.initialize(self.M, 0)
            m = dl.Vector()
            self.prior.init_vector(m, 0)
            p = TimeDependentVector(self.simulation_times)
            p.initialize(self.M, 0)
            return [u, m, p]
        elif component == STATE:
            u = TimeDependentVector(self.simulation_times)
            u.initialize(self.M, 0)
            return u
        elif component == PARAMETER:
            m = dl.Vector()
            self.prior.init_vector(m, 0)
            return m
        elif component == ADJOINT:
            p = TimeDependentVector(self.simulation_times)
            p.initialize(self.M, 0)
            return p
        else:
            raise

    def init_parameter(self, m):
        self.prior.init_vector(m, 0)

    def cost(self, x):
        reg = self.prior.cost(x[PARAMETER])

        misfit = self.misfit.cost(x)

        return [reg + misfit, reg, misfit]

    def define_Fwd_solver(self, x):
        utest = dl.TestFunction(self.Vh[ADJOINT])
        utrial = dl.TrialFunction(self.Vh[STATE])
        m = vector2Function(x[PARAMETER], self.Vh[PARAMETER])

        self.N = dl.assemble(self.pde_varf(utrial, m, utest))
        self.L = self.M + self.dt * self.N + self.stab
        self.solver = PETScLUSolver(self.mesh.mpi_comm())
        self.solver.set_operator(dl.as_backend_type(self.L))

    def solveFwd(self, out, x):
        out.zero()
        x[STATE].store(self.ic, 0)
        uold = x[STATE].data[0].copy()
        self.define_Fwd_solver(x)
        u = dl.Vector()
        rhs = dl.Vector()
        self.M.init_vector(rhs, 0)
        self.M.init_vector(u, 0)
        for t in self.simulation_times[1::]:
            self.M_stab.mult(uold, rhs)
            self.solver.solve(u, rhs)
            out.store(u, t)
            uold = u

    def define_Adj_solver(self, x):
        utest = dl.TestFunction(self.Vh[ADJOINT])
        utrial = dl.TrialFunction(self.Vh[STATE])
        m = vector2Function(x[PARAMETER], self.Vh[PARAMETER])

        self.Nt = dl.assemble(self.pde_varf(utest, m, utrial))
        self.Lt = self.M + self.dt * self.Nt + self.stab
        self.solvert = dl.PETScLUSolver(self.mesh.mpi_comm())
        self.solvert.set_operator(dl.as_backend_type(self.Lt))

    def solveAdj(self, out, x):

        grad_state = TimeDependentVector(self.simulation_times)
        grad_state.initialize(self.M, 0)
        self.misfit.grad(STATE, x, grad_state)

        self.define_Adj_solver(x)

        out.zero()

        pold = dl.Vector()
        p = dl.Vector()
        rhs = dl.Vector()
        grad_state_snap = dl.Vector()

        self.M.init_vector(pold, 0)
        self.M.init_vector(p, 0)
        self.M.init_vector(rhs, 0)
        self.M.init_vector(grad_state_snap, 0)

        rhs = dl.Vector()
        for t in self.simulation_times[::-1]:
            self.Mt_stab.mult(pold, rhs)
            grad_state.retrieve(grad_state_snap, t)
            rhs.axpy(-1., grad_state_snap)
            self.solvert.solve(p, rhs)
            pold = p
            out.store(p, t)

    def Rsolver(self):
        """
        Return an object :code:`Rsovler` that is a suitable solver for the regularization
        operator :math:`R`.

        The solver object should implement the method :code:`Rsolver.solve(z,r)` such that
        :math:`Rz \approx r`.
        """
        return self.prior.Rsolver

    def updategrade(self, out, x):
        m = vector2Function(x[PARAMETER], self.Vh[PARAMETER])
        utemp = dl.Vector()
        ptemp = dl.Vector()
        utemp.init(self.Vh[STATE].dim())  # utemp right size
        ptemp.init(self.Vh[ADJOINT].dim())  # utemp right size

        u = x[STATE].copy()
        p = x[ADJOINT].copy()

        out.zero()

        for t in self.simulation_times[1::]:
            u.retrieve(utemp, t)  # assign values to utemp = u(x, t)
            p.retrieve(ptemp, t)  # assign values to utemp = p(x, t)
            ut = dl.Function(self.Vh[STATE], utemp)  # as a function
            pt = dl.Function(self.Vh[ADJOINT], ptemp)  # as a function

            out += dl.assemble(dl.derivative(self.pde_varf(ut, m, pt), m))

        out *= self.dt

    def evalGradientParameter(self, x, mg, misfit_only=False):
        self.prior.init_vector(mg, 1)
        if misfit_only == False:
            detm = x[PARAMETER] - self.prior.mean
            self.prior.R.mult(detm, mg)
        else:
            mg.zero()

        mg1 = dl.Vector()
        mg1.init(self.Vh[PARAMETER].dim())
        self.updategrade(mg1, x)
        mg.axpy(1., mg1)

        g = dl.Vector()
        g.init(self.Vh[PARAMETER].dim())
        self.prior.Msolver.solve(g, mg)
        grad_norm = g.inner(mg)
        return grad_norm

    def setPointForHessianEvaluations(self, x, gauss_newton_approx=True):
        """
        Specify the point x = [u,m,p] at which the Hessian operator (or the Gauss-Newton approximation)
        need to be evaluated.

        """
        self.x = x
        self.gauss_newton_approx = gauss_newton_approx

    def solveFwdIncremental(self, sol, rhs):
        sol.zero()
        uold = dl.Vector()
        u = dl.Vector()
        Muold = dl.Vector()
        myrhs = dl.Vector()
        self.M.init_vector(uold, 0)
        self.M.init_vector(u, 0)
        self.M.init_vector(Muold, 0)
        self.M.init_vector(myrhs, 0)
        u.zero()
        t = self.simulation_times[0]
        sol.store(u, t)

        uold = u
        for t in self.simulation_times[1::]:
            self.M_stab.mult(uold, Muold)
            rhs.retrieve(myrhs, t)
            Muold.axpy(-1., myrhs)
            self.solver.solve(u, Muold)
            sol.store(u, t)
            uold = u

    def solveAdjIncremental(self, sol, rhs):

        sol.zero()
        pold = dl.Vector()
        p = dl.Vector()
        Mpold = dl.Vector()
        myrhs = dl.Vector()
        self.M.init_vector(pold, 0)
        self.M.init_vector(p, 0)
        self.M.init_vector(Mpold, 0)
        self.M.init_vector(myrhs, 0)

        for t in self.simulation_times[::-1]:
            self.Mt_stab.mult(pold, Mpold)
            rhs.retrieve(myrhs, t)
            Mpold.axpy(-1., myrhs)
            self.solvert.solve(p, Mpold)
            pold = p
            sol.store(p, t)


    def apply_ij(self, i, j, direction, out):
        out.zero()

        if not (i == STATE and j == STATE):

            methodType = 1

            m = vector2Function(self.x[PARAMETER], self.Vh[PARAMETER])
            utemp = dl.Vector()
            ptemp = dl.Vector()
            utemp.init(self.Vh[STATE].dim())  # utemp right size
            ptemp.init(self.Vh[ADJOINT].dim())  # utemp right size

            if i == STATE and j == PARAMETER:

                myout = dl.Vector()
                self.M.init_vector(myout, 0)
                myout.zero()
                t = self.simulation_times[0]
                out.store(myout, t)

                for t in self.simulation_times[1::]:
                    myout.zero()

                    self.x[STATE].retrieve(utemp, t)
                    self.x[ADJOINT].retrieve(ptemp, t)

                    ut = vector2Function(utemp, self.Vh[STATE])
                    pt = vector2Function(ptemp, self.Vh[ADJOINT])

                    if methodType == 1:
                        dir = vector2Function(direction, self.Vh[PARAMETER])
                        dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), m, dir), ut), tensor=myout)
                    else:
                        Wum = dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), ut), m))
                        Wum.mult(direction, myout)

                    myout *= self.dt
                    out.store(myout, t)


            elif i == ADJOINT and j == PARAMETER:

                myout = dl.Vector()
                myout.init(self.Vh[ADJOINT].dim())
                myout.zero()
                t = self.simulation_times[0]
                out.store(myout, t)

                for t in self.simulation_times[1::]:
                    myout.zero()

                    self.x[STATE].retrieve(utemp, t)
                    self.x[ADJOINT].retrieve(ptemp, t)

                    ut = vector2Function(utemp, self.Vh[STATE])
                    pt = vector2Function(ptemp, self.Vh[ADJOINT])

                    if methodType == 1:
                        dir = vector2Function(direction, self.Vh[PARAMETER])
                        dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), m, dir), pt), tensor=myout)
                    else:
                        Wpm = dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), pt), m))
                        Wpm.mult(direction, myout)

                    myout *= self.dt
                    out.store(myout, t)


            elif i == PARAMETER and j == PARAMETER:

                myout = dl.Vector()
                myout.init(self.Vh[PARAMETER].dim())
                dut = dl.Vector()
                self.M.init_vector(dut, 0)

                for t in self.simulation_times[1::]:
                    myout.zero()

                    self.x[STATE].retrieve(utemp, t)
                    self.x[ADJOINT].retrieve(ptemp, t)

                    ut = vector2Function(utemp, self.Vh[STATE])
                    pt = vector2Function(ptemp, self.Vh[ADJOINT])

                    if methodType == 1:
                        dir = vector2Function(direction, self.Vh[PARAMETER])
                        dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), m, dir), m), tensor=myout)
                    else:
                        Wmm = dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), m), m))
                        Wmm.mult(direction, myout)

                    out += myout
                out *= self.dt


            elif i == PARAMETER and j == ADJOINT:

                myout = dl.Vector()
                myout.init(self.Vh[PARAMETER].dim())
                myout.zero()
                dpt = dl.Vector()
                self.M.init_vector(dpt, 0)

                for t in self.simulation_times[1::]:
                    myout.zero()
                    direction.retrieve(dpt, t)

                    self.x[STATE].retrieve(utemp, t)
                    self.x[ADJOINT].retrieve(ptemp, t)

                    ut = vector2Function(utemp, self.Vh[STATE])
                    pt = vector2Function(ptemp, self.Vh[ADJOINT])

                    if methodType == 1:
                        dir = vector2Function(dpt, self.Vh[STATE])
                        dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), pt, dir), m), tensor=myout)
                    else:
                        Wmp = dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), m), pt))
                        Wmp.mult(dpt, myout)

                    out += myout
                out *= self.dt


            elif i == PARAMETER and j == STATE:

                myout = dl.Vector()
                myout.init(self.Vh[PARAMETER].dim())
                dut = dl.Vector()
                self.M.init_vector(dut, 0)

                for t in self.simulation_times[1::]:
                    myout.zero()
                    direction.retrieve(dut, t)

                    self.x[STATE].retrieve(utemp, t)
                    self.x[ADJOINT].retrieve(ptemp, t)

                    ut = vector2Function(utemp, self.Vh[STATE])
                    pt = vector2Function(ptemp, self.Vh[ADJOINT])

                    if methodType == 1:
                        dir = vector2Function(dut, self.Vh[STATE])
                        dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), ut, dir), m), tensor=myout)
                    else:
                        Wmu = dl.assemble(dl.derivative(dl.derivative(self.pde_varf(ut, m, pt), m), ut))
                        Wmu.mult(dut, myout)

                    out += myout
                out *= self.dt

        else:
            pass

    def applyC(self, dm, out):
        out.zero()
        self.apply_ij(ADJOINT, PARAMETER, dm, out)

    def applyCt(self, dp, out):
        out.zero()
        self.apply_ij(PARAMETER, ADJOINT, dp, out)

    def applyWuu(self, du, out):
        out.zero()
        self.misfit.apply_ij(STATE, STATE, du, out)

    def applyWum(self, dm, out):
        out.zero()
        self.apply_ij(STATE, PARAMETER, dm, out)

    def applyWmu(self, du, out):
        out.zero()
        self.apply_ij(PARAMETER, STATE, du, out)

    def applyR(self, dm, out):
        self.prior.R.mult(dm, out)

    def applyWmm(self, dm, out):
        out.zero()
        self.apply_ij(PARAMETER, PARAMETER, dm, out)

    def exportState(self, x, filename, varname):
        out_file = dl.XDMFFile(self.Vh[STATE].mesh().mpi_comm(), filename)
        out_file.parameters["functions_share_mesh"] = True
        out_file.parameters["rewrite_function_mesh"] = False
        ufunc = dl.Function(self.Vh[STATE], name=varname)
        t = self.simulation_times[0]
        out_file.write(vector2Function(x[PARAMETER], self.Vh[STATE], name=varname), t)
        for t in self.simulation_times[1:]:
            x[STATE].retrieve(ufunc.vector(), t)
            out_file.write(ufunc, t)

def v_boundary(x, on_boundary):
    return on_boundary


def q_boundary(x, on_boundary):
    return x[0] < dl.DOLFIN_EPS and x[1] < dl.DOLFIN_EPS


def computeVelocityField(mesh):
    Xh = dl.VectorFunctionSpace(mesh, 'Lagrange', 2)
    Wh = dl.FunctionSpace(mesh, 'Lagrange', 1)

    mixed_element = dl.MixedElement([Xh.ufl_element(), Wh.ufl_element()])
    XW = dl.FunctionSpace(mesh, mixed_element)

    Re = 1e2

    g = dl.Expression(('0.0', '(x[0] < 1e-14) - (x[0] > 1 - 1e-14)'), element=Xh.ufl_element())
    bc1 = dl.DirichletBC(XW.sub(0), g, v_boundary)
    bc2 = dl.DirichletBC(XW.sub(1), dl.Constant(0), q_boundary, 'pointwise')
    bcs = [bc1, bc2]

    vq = dl.Function(XW)
    (v, q) = ufl.split(vq)
    (v_test, q_test) = dl.TestFunctions(XW)

    def strain(v):
        return ufl.sym(ufl.grad(v))

    F = ((2. / Re) * ufl.inner(strain(v), strain(v_test)) + ufl.inner(ufl.nabla_grad(v) * v, v_test)
         - (q * ufl.div(v_test)) + (ufl.div(v) * q_test)) * ufl.dx

    dl.solve(F == 0, vq, bcs, solver_parameters={"newton_solver":
                                                     {"relative_tolerance": 1e-4, "maximum_iterations": 100,
                                                      "linear_solver": "default"}})

    return v


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Model Advection Diffusion')
    parser.add_argument('--mesh',
                        default="ad_10k.xml",
                        type=str,
                        help="Mesh filename")
    parser.add_argument('--nref',
                        default=0,
                        type=int,
                        help="Number of uniform mesh refinements")
    args = parser.parse_args()
    try:
        dl.set_log_active(False)
    except:
        pass
    np.random.seed(1)
    sep = "\n" + "#" * 80 + "\n"

    nref = args.nref

    mesh = dl.Mesh(args.mesh)
    for i in range(nref):
        mesh = dl.refine(mesh)

    rank = dl.MPI.rank(mesh.mpi_comm())
    nproc = dl.MPI.size(mesh.mpi_comm())

    if rank == 0:
        print(sep, "Set up the mesh and finite element spaces.\n", "Compute wind velocity", sep)
    Vh = dl.FunctionSpace(mesh, "Lagrange", 2)
    ndofs = Vh.dim()
    if rank == 0:
        print("Number of dofs: {0}".format(ndofs))

    if rank == 0:
        print(sep, "Set up Prior Information and model", sep)

    ic_expr = dl.Expression('min(0.5,exp(-100*(pow(x[0]-0.35,2) +  pow(x[1]-0.7,2))))', element=Vh.ufl_element())
    true_initial_condition = dl.interpolate(ic_expr, Vh).vector()

    gamma = 1.
    delta = 8.
    prior = BiLaplacianPrior(Vh, gamma, delta, robin_bc=True)
    if rank == 0:
        print("Prior regularization: (delta - gamma*Laplacian)^order: delta={0}, gamma={1}, order={2}".format(delta,
                                                                                                              gamma, 2))

    prior.mean = dl.interpolate(dl.Constant(0.25), Vh).vector()

    t_init = 0.
    t_final = 4.
    t_1 = 1.
    dt = .1
    observation_dt = .2

    simulation_times = np.arange(t_init, t_final + .5 * dt, dt)
    observation_times = np.arange(t_1, t_final + .5 * dt, observation_dt)

    targets = np.loadtxt('targets.txt')
    if rank == 0:
        print("Number of observation points: {0}".format(targets.shape[0]))
    misfit = SpaceTimePointwiseStateObservation(Vh, observation_times, targets)

    wind_velocity = computeVelocityField(mesh)

    problem = TimeDependentAD(mesh, [Vh, Vh, Vh], prior, misfit, simulation_times, wind_velocity, True)

    if rank == 0:
        print(sep, "Generate synthetic observation", sep)
    rel_noise = 0.01
    utrue = problem.generate_vector(STATE)
    x = [utrue, true_initial_condition, None]
    problem.solveFwd(x[STATE], x)
    misfit.observe(x, misfit.d)
    MAX = misfit.d.norm("linf", "linf")
    noise_std_dev = rel_noise * MAX
    parRandom.normal_perturb(noise_std_dev, misfit.d)
    misfit.noise_variance = noise_std_dev * noise_std_dev

    if rank == 0:
        print(sep, "Test the gradient and the Hessian of the model", sep)
    m0 = true_initial_condition.copy()
    modelVerify(problem, m0, is_quadratic=True, misfit_only=True, verbose=(rank == 0))

    if rank == 0:
        print(sep, "Compute the reduced gradient and hessian", sep)
    [u, m, p] = problem.generate_vector()
    problem.solveFwd(u, [u, m, p])
    problem.solveAdj(p, [u, m, p])
    mg = problem.generate_vector(PARAMETER)
    grad_norm = problem.evalGradientParameter([u, m, p], mg)

    if rank == 0:
        print("(g,g) = ", grad_norm)

    if rank == 0:
        print(sep, "Compute the low rank Gaussian Approximation of the posterior", sep)

    H = ReducedHessian(problem, misfit_only=True)
    k = 80
    p = 20
    if rank == 0:
        print("Double Pass Algorithm. Requested eigenvectors: {0}; Oversampling {1}.".format(k, p))

    Omega = MultiVector(x[PARAMETER], k + p)
    parRandom.normal(1., Omega)

    d, U = doublePassG(H, prior.R, prior.Rsolver, Omega, k, s=1, check=False)
    posterior = GaussianLRPosterior(prior, d, U)

    if True:
        P = posterior.Hlr
    else:
        P = prior.Rsolver

    if rank == 0:
        print(sep, "Find the MAP point", sep)

    H.misfit_only = False

    solver = CGSolverSteihaug()
    solver.set_operator(H)
    solver.set_preconditioner(P)
    solver.parameters["print_level"] = 1
    solver.parameters["rel_tolerance"] = 1e-6
    if rank != 0:
        solver.parameters["print_level"] = -1
    solver.solve(m, -mg)
    problem.solveFwd(u, [u, m, p])

    total_cost, reg_cost, misfit_cost = problem.cost([u, m, p])
    if rank == 0:
        print("Total cost {0:5g}; Reg Cost {1:5g}; Misfit {2:5g}".format(total_cost, reg_cost, misfit_cost))

    posterior.mean = m

    compute_trace = False
    if compute_trace:
        post_tr, prior_tr, corr_tr = posterior.trace(method="Randomized", r=200)
        if rank == 0:
            print("Posterior trace {0:5g}; Prior trace {1:5g}; Correction trace {2:5g}".format(post_tr, prior_tr,
                                                                                               corr_tr))
    post_pw_variance, pr_pw_variance, corr_pw_variance = posterior.pointwise_variance(method="Randomized", r=200)

    if rank == 0:
        print(sep, "Save results", sep)
    problem.exportState([u, m, p], "results/conc.xdmf", "concentration")
    problem.exportState([utrue, true_initial_condition, p], "results/true_conc.xdmf", "concentration")

    with dl.XDMFFile(mesh.mpi_comm(), "results/pointwise_variance.xdmf") as fid:
        fid.parameters["functions_share_mesh"] = True
        fid.parameters["rewrite_function_mesh"] = False

        fid.write(vector2Function(post_pw_variance, Vh, name="Posterior"), 0)
        fid.write(vector2Function(pr_pw_variance, Vh, name="Prior"), 0)
        fid.write(vector2Function(corr_pw_variance, Vh, name="Correction"), 0)

    U.export(Vh, "results/evect.xdmf", varname="gen_evect", normalize=True)
    if rank == 0:
        np.savetxt("results/eigevalues.dat", d)

    fid_prmean = dl.XDMFFile(mesh.mpi_comm(), "results/pr_mean.xdmf")
    fid_prmean.write(vector2Function(prior.mean, Vh, name="prior mean"))

    if rank == 0:
        print(sep, "Generate samples from Prior and Posterior", sep)

    nsamples = 50
    noise = dl.Vector()
    posterior.init_vector(noise, "noise")
    s_prior = dl.Function(Vh, name="sample_prior")
    s_post = dl.Function(Vh, name="sample_post")
    with dl.XDMFFile(mesh.mpi_comm(), "results/samples.xdmf") as fid:
        fid.parameters["functions_share_mesh"] = True
        fid.parameters["rewrite_function_mesh"] = False
        for i in range(nsamples):
            parRandom.normal(1., noise)
            posterior.sample(noise, s_prior.vector(), s_post.vector())
            fid.write(s_prior, i)
            fid.write(s_post, i)

    if rank == 0:
        print(sep, "Visualize results", sep)
        plt.figure()
        plt.plot(range(0, k), d, 'b*', range(0, k), np.ones(k), '-r')
        plt.yscale('log')
        plt.show()

