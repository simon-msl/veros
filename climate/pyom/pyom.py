import math
import argparse
import warnings
import logging

import numpy
if numpy.__name__ == "bohrium":
    warnings.warn("Running pyOM with -m bohrium is discouraged (use --backend bohrium instead)")
    import numpy_force
    numpy = numpy_force
try:
    import bohrium
    import bohrium.lapack
except ImportError:
    warnings.warn("Could not import Bohrium")
    bohrium = None

BACKENDS = {"numpy": numpy, "bohrium": bohrium}

from climate import Timer
from climate.pyom import momentum, numerics, thermodynamics, eke, tke, idemix, \
                         isoneutral, external, diagnostics, non_hydrostatic, \
                         advection, restart, cyclic, variables, settings

class PyOM(object):
    """Main class for PyOM.

    Args:
        backend (bool, optional): Backend to use for array operations.
            Possible values are `numpy` and `bohrium`. Defaults to `None`, which
            tries to read the backend from the command line (set via a flag
            `-b`/`--backend`), and uses `numpy` if no command line argument is given.
    """

    # Constants
    pi = numpy.pi
    radius = 6370.0e3 #: Earth radius in m
    degtom = radius / 180.0 * pi #: Conversion degrees latitude to meters
    mtodeg = 1 / degtom #: Conversion meters to degrees latitude
    omega = pi / 43082.0 #: Earth rotation frequency in 1/s
    rho_0 = 1024.0 #: Boussinesq reference density in :math`kg/m^3`
    grav = 9.81 #: Gravitational constant in :math:`m/s^2`

    # Interface
    def _not_implemented(self):
        raise NotImplementedError("Needs to be implemented by subclass")
    set_parameter = _not_implemented
    set_initial_conditions = _not_implemented
    set_grid = _not_implemented
    set_coriolis = _not_implemented
    set_forcing = _not_implemented
    set_topography = _not_implemented
    set_diagnostics = _not_implemented

    def __init__(self, backend=None, loglevel=None, logfile=None):
        args = self._parse_command_line()
        self.backend, self.backend_name = self._get_backend(backend or args.backend)
        logging.basicConfig(logfile=logfile or args.logfile, filemode="w",
                            level=getattr(logging, (loglevel or args.loglevel).upper()),
                            format="%(message)s")
        self.profile_mode = args.profile
        self.set_default_settings()
        self.timers = {k: Timer(k) for k in ("setup","main","momentum","temperature",
                                             "eke","idemix","tke","diagnostics",
                                             "pressure","friction","isoneutral",
                                             "vmix","eq_of_state")}

    def _parse_command_line(self):
        parser = argparse.ArgumentParser(description="PyOM command line interface")
        parser.add_argument("--backend", "-b", default="numpy", choices=BACKENDS.keys(),
                            help="Backend to use for computations. Defaults to 'numpy'.")
        parser.add_argument("--loglevel", "-v", default="info",
                            choices=("debug","info","warning","error","critical"),
                            help="Log level used for output. Defaults to 'info'.")
        parser.add_argument("--logfile", "-l", default=None,
                            help="Log file to write to. Writing to stdout if not set.")
        parser.add_argument("--profile", "-p", default=False, action="store_true",
                            help="Profile PyOM using pyinstrument")
        args, _ = parser.parse_known_args()
        return args

    def _get_backend(self, backend):
        if not backend in BACKENDS.keys():
            raise ValueError("unrecognized backend {} (must be either of: {!r})".format(backend, BACKENDS.keys()))
        if BACKENDS[backend] is None:
            raise ValueError("{} backend failed to import".format(backend))
        return BACKENDS[backend], backend


    def set_default_settings(self):
        for key, setting in settings.SETTINGS.items():
            setattr(self, key, setting.default)


    def allocate(self):
        self.variables = {}
        def init_var(var_name, var):
            shape = variables.get_dimensions(self, var.dims)
            setattr(self, var_name, self.backend.zeros(shape, dtype=var.dtype))
            self.variables[var_name] = var
        for var_name, var in variables.MAIN_VARIABLES.items():
            init_var(var_name, var)
        for condition, var_dict in variables.CONDITIONAL_VARIABLES.items():
            if condition.startswith("not "):
                eval_condition = not bool(getattr(self, condition[4:]))
            else:
                eval_condition = bool(getattr(self, condition))
            if eval_condition:
                for var_name, var in var_dict.items():
                    init_var(var_name, var)


    def flush(self):
        try:
            self.backend.flush()
        except AttributeError:
            pass


    def run(self, snapint, runlen):
        self.runlen = runlen
        self.snapint = snapint

        with self.timers["setup"]:
            """
            Initialize model
            """
            self.setup()

            """
            read restart if present
            """
            print("Reading restarts:")
            restart.read_restart(self.itt)

            if self.enable_diag_averages:
                diagnostics.diag_averages_read_restart(self)
            if self.enable_diag_energy:
                diagnostics.diag_energy_read_restart(self)
            if self.enable_diag_overturning:
                diagnostics.diag_over_read_restart(self)
            if self.enable_diag_particles:
                diagnostics.diag_particles_read_restart(self)

            self.enditt = self.itt + int(self.runlen / self.dt_tracer)
            logging.info("Starting integration for {:.2e}s".format(self.runlen))
            logging.info(" from time step {} to {}".format(self.itt,self.enditt))


        try:
            while self.itt < self.enditt:
                if self.itt == 3 and self.profile_mode:
                    import pyinstrument
                    profiler = pyinstrument.Profiler()
                    profiler.start()

                with self.timers["main"]:
                    self.set_forcing()

                    if self.enable_idemix:
                        idemix.set_idemix_parameter(self)
                    if self.enable_idemix_M2 or self.enable_idemix_niw:
                        idemix.set_spectral_parameter(self)

                    eke.set_eke_diffusivities(self)
                    tke.set_tke_diffusivities(self)

                    with self.timers["momentum"]:
                        momentum.momentum(self)

                    with self.timers["temperature"]:
                        thermodynamics.thermodynamics(self)

                    if self.enable_eke or self.enable_tke or self.enable_idemix:
                        advection.calculate_velocity_on_wgrid(self)

                    with self.timers["eke"]:
                        if self.enable_eke:
                            eke.integrate_eke(self)

                    with self.timers["idemix"]:
                        if self.enable_idemix_M2:
                            idemix.integrate_idemix_M2(self)
                        if self.enable_idemix_niw:
                            idemix.integrate_idemix_niw(self)
                        if self.enable_idemix:
                            idemix.integrate_idemix(self)
                        if self.enable_idemix_M2 or self.enable_idemix_niw:
                            idemix.wave_interaction(self)

                    with self.timers["tke"]:
                        if self.enable_tke:
                            tke.integrate_tke(self)

                    """
                    Main boundary exchange
                    """
                    if self.enable_cyclic_x:
                        cyclic.setcyclic_x(self.u[:,:,:,self.taup1])
                        cyclic.setcyclic_x(self.v[:,:,:,self.taup1])
                        if self.enable_tke:
                            cyclic.setcyclic_x(self.tke[:,:,:,self.taup1])
                        if self.enable_eke:
                            cyclic.setcyclic_x(self.eke[:,:,:,self.taup1])
                        if self.enable_idemix:
                            cyclic.setcyclic_x(self.E_iw[:,:,:,self.taup1])
                        if self.enable_idemix_M2:
                            cyclic.setcyclic_x(self.E_M2[:,:,:,self.taup1])
                        if self.enable_idemix_niw:
                            cyclic.setcyclic_x(self.E_niw[:,:,:,self.taup1])

                    # diagnose vertical velocity at taup1
                    if self.enable_hydrostatic:
                        momentum.vertical_velocity(self)

                self.flush()

                with self.timers["diagnostics"]:
                    diagnostics.diagnose(self)

                # shift time
                otaum1 = self.taum1
                self.taum1 = self.tau
                self.tau = self.taup1
                self.taup1 = otaum1
                self.itt += 1
                logging.info("Current iteration: {}".format(self.itt))
                logging.debug("Time step took {}s".format(self.timers["main"].getLastTime()))

        except:
            diagnostics.panic_snap(self)
            raise

        finally:
            logging.debug("Timing summary:")
            logging.debug(" setup time summary       = {}s".format(self.timers["setup"].getTime()))
            logging.debug(" main loop time summary   = {}s".format(self.timers["main"].getTime()))
            logging.debug("     momentum             = {}s".format(self.timers["momentum"].getTime()))
            logging.debug("       pressure           = {}s".format(self.timers["pressure"].getTime()))
            logging.debug("       friction           = {}s".format(self.timers["friction"].getTime()))
            logging.debug("     thermodynamics       = {}s".format(self.timers["temperature"].getTime()))
            logging.debug("       lateral mixing     = {}s".format(self.timers["isoneutral"].getTime()))
            logging.debug("       vertical mixing    = {}s".format(self.timers["vmix"].getTime()))
            logging.debug("       equation of state  = {}s".format(self.timers["eq_of_state"].getTime()))
            logging.debug("     EKE                  = {}s".format(self.timers["eke"].getTime()))
            logging.debug("     IDEMIX               = {}s".format(self.timers["idemix"].getTime()))
            logging.debug("     TKE                  = {}s".format(self.timers["tke"].getTime()))
            logging.debug(" diagnostics              = {}s".format(self.timers["diagnostics"].getTime()))

            if self.profile_mode:
                try:
                    profiler.stop()
                    with open("profile.html", "w") as f:
                        f.write(profiler.output_html())
                except UnboundLocalError:
                    pass


    def setup(self):
        print("setting up everything")

        """
        allocate everything
        """
        self.set_parameter()
        self.allocate()

        """
        Grid
        """
        self.set_grid()
        numerics.calc_grid(self)

        """
        Coriolis
        """
        self.set_coriolis()
        numerics.calc_beta(self)

        """
        topography
        """
        self.set_topography()
        numerics.calc_topo(self)
        idemix.calc_spectral_topo(self)

        """
        initial condition and forcing
        """
        self.set_initial_conditions()
        numerics.calc_initial_conditions(self)

        self.set_forcing()
        if self.enable_streamfunction:
            external.streamfunction_init(self)

        """
        initialize diagnostics
        """
        self.set_diagnostics()
        diagnostics.init_diagnostics(self)

        """
        initialize EKE module
        """
        eke.init_eke(self)

        """
        initialize isoneutral module
        """
        isoneutral.check_isoneutral_slope_crit(self)

        """
        check setup
        """
        if self.enable_tke and not self.enable_implicit_vert_friction:
            raise RuntimeError("ERROR: use TKE model only with implicit vertical friction\n"
                               "\t-> switch on enable_implicit_vert_fricton in setup")
