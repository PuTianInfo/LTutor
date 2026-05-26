/**
 * site-tour.js
 *
 * Purpose:
 *   Controls the guided tours for both student and instructor dashboards.
 *   Defines role-specific tour steps and initializes driver.js with the
 *   correct configuration.
 *
 * Used by:
 *   - Student dashboard (student.html)
 *   - Instructor dashboard (instructor.html)
 *
 * Dependencies:
 *   - driver.js (third-party guided-tour library)
 */
class SiteTour {
  constructor(role) {
    this.role = role; // 'student' or 'instructor'

    // Initialize Driver.js controller
    this.driver = driver.js.driver({
      animate: true,
      opacity: 0.75,
      allowClose: true,
      keyboardControl: true
    });

    // Pre-build the step list based on the role
    this.steps = this.getSteps();
  }

    // Return an array of tour steps depending on user role
    // Each step has:
    //  - a CSS selector of the target element
    //  - a popover containing a title, desciption, and postion
    getSteps() {
        // Student steps
        if (this.role === 'student') {
            return [
                {
                    element: '.navbar-left',
                    popover: {
                        title: 'LeetTutor Dashboard',
                        description: 'Welcome to your student dashboard. Here you can access all your coding projects and AI tutoring.',
                        position: 'bottom'
                    }
                },
                {
                    element: '.container .sidebar',
                    popover: {
                        title: 'Project Navigation',
                        description: 'Manage and switch between your coding projects. Each project represents an assignment or learning task.',
                        position: 'right'
                    }
                },
                {
                    element: '.chat',
                    popover: {
                        title: 'AI Tutor Chat',
                        description: 'Interact with your AI tutor. Ask questions, get coding help, and receive personalized guidance.',
                        position: 'left'
                    }
                },
                {
                    element: '.editor',
                    popover: {
                        title: 'Integrated Code Editor',
                        description: 'Write, run, and debug your code directly in the browser. Multiple language support available.',
                        position: 'top'
                    }
                }
            ];
        // Instructor steps
        } else if (this.role === 'instructor') {
            return [
                {
                    element: '.navbar-left',
                    popover: {
                        title: 'Instructor Dashboard',
                        description: 'Welcome to your LeetTutor instructor control center. Manage classes, assignments, and student progress.',
                        position: 'bottom'
                    }
                },
                {
                    element: '.sidebar',
                    popover: {
                        title: 'Class Management',
                        description: 'Use this sidebar to view, create, and manage your classes.',
                        position: 'right'
                    }
                },
                {
                    element: '[onclick="addProject()"]',
                    popover: {
                        title: 'Create New Class',
                        description: 'Click here to add a new class and start setting up assignments.',
                        position: 'bottom'
                    }
                },
                {
                    element: '#rosterSection',
                    popover: {
                        title: 'Student Roster',
                        description: 'Manage your class roster. Add students individually or upload a CSV file.',
                        position: 'left'
                    }
                },
                {
                    element: '#assignSection',
                    popover: {
                        title: 'Assignments',
                        description: 'Create, manage, and track assignments for your class.',
                        position: 'right'
                    }
                }
            ];
        }
    }

    // First step of the "Tour on First Login" system
    // Checks if this is the user's first login    
    // Legacy Code, keeping for future implementation
    async checkFirstLogin() {
        try {
            const response = await fetch(`/api/check_first_login_${this.role}`);
            const data = await response.json();
            
            if (data.is_first_login) {
                this.showTour(true);
                this.markFirstLoginComplete();
            }
        } catch (error) {
            console.error(`Error checking first login for ${this.role}:`, error);
        }
    }

    // Second step of the "Tour on First Login" system
    // Shows site tour if this is the user's first login    
    // Legacy Code, keeping for future implementation
    showTour(isFirstTime = false) {
        const validSteps = this.steps.filter(step => {
            const element = document.querySelector(step.element);
            if (!element) {
                console.warn(`Tour step element not found: ${step.element}`);
                return false;
            }
            return true;
        });

        if (validSteps.length > 0) {
            this.driver.setSteps(validSteps);
            this.driver.drive();
        } else {
            console.error('No valid tour steps found');
        }
    }

    // Last step of the "Tour on First Login" system
    // Marks that the first login tour was completed    
    // Legacy Code, keeping for future implementation
    async markFirstLoginComplete() {
        try {
            await fetch(`/api/mark_first_login_${this.role}`, {
                method: 'POST'
            });
        } catch (error) {
            console.error(`Error marking first login for ${this.role}:`, error);
        }
    }
}

// -------------------------------------------------
// Initialize site tour on DOM load
// -------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  console.log("DOMContentLoaded: Site Tour Script");

  // Detect role based on body class
  const isInstructorPage = document.body.classList.contains("instructor-page");
  const detectedRole = isInstructorPage ? "instructor" : "student";
  console.log("🎓 Role Detected:", { isInstructorPage, detectedRole });

  // Pick the correct "Show Tour" button
  const tourButton = document.getElementById(
  detectedRole === "instructor" ? "instructor-tour-trigger" : "student-tour-trigger"
);

  const siteTour = new SiteTour(detectedRole);

  // Manual tour trigger
  if (tourButton) {
    tourButton.addEventListener("click", () => {
      console.log("🚀 Starting tour...");
      siteTour.showTour();
    });
  } else {
    console.warn("⚠️ Show Tour button not found");
  }

  // Enable automatic first-login tours
  // Legacy Code, keeping for future implementation
  // siteTour.checkFirstLogin();
});
